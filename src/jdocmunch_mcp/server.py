"""MCP server for jdocmunch-mcp."""

import argparse
import asyncio
import json
import os
import sys
import traceback
from typing import Any, Optional

from mcp.server import Server
from mcp.types import Tool, TextContent

from .tools.index_local import index_local
from .tools.index_repo import index_repo
from .tools.list_repos import list_repos
from .tools.get_repo_overview import get_repo_overview
from .tools.get_document_outline import get_document_outline
from .tools.search_sections import search_sections
from .tools.get_section import get_section
from .tools.get_sections import get_sections
from .tools.delete_index import delete_index
from .auto_refresh import auto_refresh as _auto_refresh

server = Server("jdocmunch-mcp")

READ_TOOLS = {
    "get_repo_overview", "get_document_outline",
    "search_sections", "get_section", "get_sections",
}


async def _refresh(repo: str, storage_path: Optional[str]) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _auto_refresh, repo, storage_path)


@server.list_tools()
async def list_tools() -> list[Tool]:
    """List all available tools."""
    return [
        Tool(
            name="index_local",
            description="Index a local folder containing documentation files (.md, .rst, .txt, .adoc, .ipynb, .html, and more). Parses by heading hierarchy into sections for efficient retrieval.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to local folder (absolute or relative, supports ~ for home directory)"
                    },
                    "extra_ignore_patterns": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Additional gitignore-style patterns to exclude from indexing"
                    },
                    "follow_symlinks": {
                        "type": "boolean",
                        "description": "Whether to follow symlinks. Default false for security.",
                        "default": False
                    }
                },
                "required": ["path"]
            }
        ),
        Tool(
            name="index_repo",
            description="Index a GitHub repository's documentation. Fetches .md, .rst, .txt, .adoc, .ipynb, .html, and more; parses sections, and saves to local storage.",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "GitHub repository URL or owner/repo string"
                    }
                },
                "required": ["url"]
            }
        ),
        Tool(
            name="list_repos",
            description="List all indexed documentation repositories.",
            inputSchema={
                "type": "object",
                "properties": {}
            }
        ),
        Tool(
            name="get_repo_overview",
            description="Get a lightweight overview of all documents in a repo — one entry per file with its top-level title and section count. Use to orient to an unfamiliar repo before searching or drilling into specific docs.",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Repository identifier (owner/repo or just repo name)"
                    }
                },
                "required": ["repo"]
            }
        ),
        Tool(
            name="get_document_outline",
            description="Get the section hierarchy for a single document file, without content.",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Repository identifier"
                    },
                    "doc_path": {
                        "type": "string",
                        "description": "Path to the document within the repository (e.g., 'README.md')"
                    }
                },
                "required": ["repo", "doc_path"]
            }
        ),
        Tool(
            name="search_sections",
            description="Search sections by weighted scoring across title, summary, tags, and content. Returns summaries only — use get_section for full content.",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Repository identifier"
                    },
                    "query": {
                        "type": "string",
                        "description": "Search query"
                    },
                    "doc_path": {
                        "type": "string",
                        "description": "Optional: limit search to a specific document"
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results to return",
                        "default": 10
                    }
                },
                "required": ["repo", "query"]
            }
        ),
        Tool(
            name="get_section",
            description="Retrieve the full content of a specific section using byte-range reads. Use after identifying section IDs via search_sections, get_document_outline, or get_repo_overview.",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Repository identifier"
                    },
                    "section_id": {
                        "type": "string",
                        "description": "Section ID from search_sections, get_document_outline, or get_repo_overview"
                    },
                    "verify": {
                        "type": "boolean",
                        "description": "Verify content hash matches stored hash (detects source drift)",
                        "default": False
                    }
                },
                "required": ["repo", "section_id"]
            }
        ),
        Tool(
            name="get_sections",
            description="Retrieve full content for multiple sections in one call. More efficient than repeated get_section calls.",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Repository identifier"
                    },
                    "section_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of section IDs to retrieve"
                    },
                    "verify": {
                        "type": "boolean",
                        "description": "Verify content hashes",
                        "default": False
                    }
                },
                "required": ["repo", "section_ids"]
            }
        ),
        Tool(
            name="delete_index",
            description="Remove a repo index and its cached raw files.",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Repository identifier (owner/repo or just repo name)"
                    }
                },
                "required": ["repo"]
            }
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Handle tool calls."""
    storage_path = os.environ.get("DOC_INDEX_PATH")

    # Auto-refresh before read tools: check for file changes and re-index incrementally
    if name in READ_TOOLS and "repo" in arguments:
        try:
            await _refresh(arguments["repo"], storage_path)
        except Exception:
            pass  # Never let refresh failure block the tool call

    try:
        if name == "index_local":
            result = index_local(
                path=arguments["path"],
                storage_path=storage_path,
                extra_ignore_patterns=arguments.get("extra_ignore_patterns"),
                follow_symlinks=arguments.get("follow_symlinks", False),
            )
        elif name == "index_repo":
            result = await index_repo(
                url=arguments["url"],
                storage_path=storage_path,
            )
        elif name == "list_repos":
            result = list_repos(storage_path=storage_path)
        elif name == "get_repo_overview":
            result = get_repo_overview(
                repo=arguments["repo"],
                storage_path=storage_path,
            )
        elif name == "get_document_outline":
            result = get_document_outline(
                repo=arguments["repo"],
                doc_path=arguments["doc_path"],
                storage_path=storage_path,
            )
        elif name == "search_sections":
            result = search_sections(
                repo=arguments["repo"],
                query=arguments["query"],
                doc_path=arguments.get("doc_path"),
                max_results=arguments.get("max_results", 10),
                storage_path=storage_path,
            )
        elif name == "get_section":
            result = get_section(
                repo=arguments["repo"],
                section_id=arguments["section_id"],
                verify=arguments.get("verify", False),
                storage_path=storage_path,
            )
        elif name == "get_sections":
            result = get_sections(
                repo=arguments["repo"],
                section_ids=arguments["section_ids"],
                verify=arguments.get("verify", False),
                storage_path=storage_path,
            )
        elif name == "delete_index":
            result = delete_index(
                repo=arguments["repo"],
                storage_path=storage_path,
            )
        else:
            result = {"error": f"Unknown tool: {name}"}

        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    except Exception as e:
        print(traceback.format_exc(), file=sys.stderr)
        return [TextContent(type="text", text=json.dumps({"error": str(e)}, indent=2))]


async def run_server():
    """Run the MCP server."""
    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options()
        )


def main(argv: Optional[list] = None):
    """Main entry point."""
    parser = argparse.ArgumentParser(
        prog="jdocmunch-mcp",
        description="Run the jDocMunch MCP stdio server.",
    )
    parser.parse_args(argv)
    asyncio.run(run_server())


if __name__ == "__main__":
    main()
