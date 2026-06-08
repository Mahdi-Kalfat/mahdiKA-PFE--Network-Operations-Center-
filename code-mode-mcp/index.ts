#!/usr/bin/env node

// UTCP-MCP Bridge Entry Point
// This is the main entry point for the npx @utcp/mcp-bridge command

import util from "util";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import path from "path";
import { promises as fs } from "fs";
import { parse as parseDotEnv } from 'dotenv';
import { fileURLToPath } from 'url';
import { dirname } from 'path';
import http from "http";

import "@utcp/http";
import "@utcp/text";
import "@utcp/mcp";
import "@utcp/cli";
import "@utcp/dotenv-loader"
import "@utcp/file"

import {
    UtcpClient,
    CallTemplateSchema,
    InMemConcurrentToolRepository,
    TagSearchStrategy,
    DefaultVariableSubstitutor,
    ensureCorePluginsInitialized,
    UtcpClientConfigSerializer
} from "@utcp/sdk";
import type { UtcpClientConfig } from "@utcp/sdk";
import { CodeModeUtcpClient } from "@utcp/code-mode";
import { ContentBlock, ContentBlockSchema } from "@modelcontextprotocol/sdk/types.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

// Override info and warn logs in simple manner to keep compatibility with MCP stdio transport
console.log = (...args: any[]) => { process.stderr.write(util.format(...args) + '\n'); }
console.warn = (...args: any[]) => { process.stderr.write(util.format(...args) + '\n'); }

ensureCorePluginsInitialized();

let utcpClient: CodeModeUtcpClient | null = null;

async function main() {
    setupMcpTools();
    utcpClient = await initializeUtcpClient();
    const transport = new StdioServerTransport();
    await mcp.connect(transport);
}

const mcp = new McpServer({
    name: "CodeMode-MCP",
    version: "1.0.0",
});

/**
 * Sanitizes an identifier to be a valid TypeScript identifier.
 */
function sanitizeIdentifier(name: string): string {
    return name
        .replace(/[^a-zA-Z0-9_]/g, '_')
        .replace(/^[0-9]/, '_$&');
}

/**
 * Converts a UTCP tool name to its TypeScript interface name.
 */
function utcpNameToTsInterfaceName(utcpName: string): string {
    if (utcpName.includes('.')) {
        const parts = utcpName.split('.');
        const manualName = parts[0]!;
        const toolParts = parts.slice(1);
        const sanitizedManualName = sanitizeIdentifier(manualName);
        const toolName = toolParts.map(part => sanitizeIdentifier(part)).join('_');
        return `${sanitizedManualName}.${toolName}`;
    } else {
        return sanitizeIdentifier(utcpName);
    }
}

/**
 * Finds a tool by either UTCP name or TypeScript interface name.
 */
async function findToolByName(client: CodeModeUtcpClient, name: string): Promise<{ tool: any, utcpName: string } | null> {
    // First, try direct lookup by UTCP name
    const directTool = await client.config.tool_repository.getTool(name);
    if (directTool) {
        return { tool: directTool, utcpName: name };
    }
    
    // If not found, search through all tools to find one whose TS interface name matches
    const allTools = await client.config.tool_repository.getTools();
    for (const tool of allTools) {
        if (utcpNameToTsInterfaceName(tool.name) === name) {
            return { tool, utcpName: tool.name };
        }
    }
    
    return null;
}

async function invokeUtcpTool(toolName: string, input: any = {}): Promise<any> {
    const client = await initializeUtcpClient();
    const normalizedToolName = toolName.includes('.') ? toolName : `router_mcp.${toolName}`;
    console.log(`[code-mode-mcp] invoking UTCP tool '${normalizedToolName}' with input=${JSON.stringify(input)}`);
    const result = await client.callTool(normalizedToolName, input || {});
    console.log(`[code-mode-mcp] UTCP tool '${normalizedToolName}' returned ${JSON.stringify(result)}`);
    return result;
}

function setupMcpTools() {
    // Register MCP prompt for using the code mode server
    mcp.registerPrompt("utcp_codemode_usage", {
        title: "UTCP Code Mode Usage Guide",
        description: "Comprehensive guide on how to use the UTCP Code Mode MCP server for executing TypeScript code with tool access."
    }, async () => {
        const codeInstructions = `# UTCP Code Mode MCP Server Usage Guide

You have access to a powerful UTCP Code Mode MCP server that allows you to execute TypeScript code with direct access to registered tools.

## Workflow: Always Follow This Pattern

### 1. 🔍 DISCOVER TOOLS FIRST
**Always start by searching for relevant tools before writing code:**
- Use \`search_tools\` with a description of your task to discover the exact tool you need
- This returns only relevant tools and their TypeScript interfaces
- Do not use any general tool-listing commands; only \`search_tools\` is required to find tools

${CodeModeUtcpClient.AGENT_PROMPT_TEMPLATE}

- in the call_tool_chain code, return the result that you want to see, your code will be wrapped in an async function and executed

Remember: The power of this system comes from combining multiple tools in sophisticated TypeScript code execution workflows.`;

        return {
            messages: [{
                role: "user",
                content: {
                    type: "text",
                    text: codeInstructions
                }
            }]
        };
    });

    mcp.registerTool("search_tools", {
        title: "Search for UTCP Tools",
        description: "Searches for relevant tools based on a task description.",
        inputSchema: {
            task_description: z.string().describe("A natural language description of the task."),
            limit: z.number().optional().default(10),
        } as any,
    }, async (input: any, _extra: any) => {
        try {
            console.error(`[search_tools] Searching for: "${input.task_description}"`);
            
            // Fetch tools from neo4j-code-mode-agent HTTP endpoint
            const routerManual = await new Promise<any>((resolve, reject) => {
                http.get('http://localhost:8000/tools', (res) => {
                    let data = '';
                    res.on('data', chunk => data += chunk);
                    res.on('end', () => {
                        try {
                            const parsed = JSON.parse(data);
                            console.error(`[search_tools] Received ${parsed.tools?.length || 0} tools from neo4j-code-mode-agent`);
                            resolve(parsed);
                        } catch (e) {
                            reject(new Error(`Failed to parse JSON: ${e}`));
                        }
                    });
                }).on('error', reject);
            });
            
            if (!routerManual.tools || !Array.isArray(routerManual.tools)) {
                throw new Error('Invalid response format from neo4j-code-mode-agent');
            }
            
            // Simple keyword matching on tool names and descriptions
            const searchKeywords = input.task_description.toLowerCase().split(/\s+/);
            console.error(`[search_tools] Keywords to match: ${searchKeywords.join(', ')}`);
            
            const matchedTools = routerManual.tools.filter((tool: any) => {
                const toolName = tool.name.toLowerCase();
                const toolDesc = (tool.description || '').toLowerCase();
                const matches = searchKeywords.some((keyword: string) => 
                    toolName.includes(keyword) || toolDesc.includes(keyword)
                );
                if (matches) {
                    console.error(`[search_tools] ✓ Matched tool: ${tool.name}`);
                }
                return matches;
            }).slice(0, input.limit);
            
            console.error(`[search_tools] Found ${matchedTools.length} matching tools`);
            
            // Add router_mcp prefix to tool names for consistency
            const toolsWithInterfaces = matchedTools.map((t: any): { name: string; description: string } => ({
                name: `router_mcp.${t.name}`,
                description: t.description
            }));
            
            return {
                content: [{ type: "text" as const, text: JSON.stringify({ tools: toolsWithInterfaces }) }]
            };
        } catch (e: any) {
            console.error(`[search_tools] Error: ${e.message}`);
            return {
                isError: true,
                content: [{ type: "text" as const, text: JSON.stringify({ error: e.message }) }]
            };
        }
    });

    mcp.registerTool("sandbox_runtime_diagnostics", {
        title: "Sandbox Runtime Diagnostics",
        description: "Executes a sandbox code snippet to return the runtime global keys, manual namespace, and tool interface state.",
        inputSchema: {} as any,
    }, async (_input: any, _extra: any) => {
        const client = await initializeUtcpClient();
        try {
            const code = `
                const globalKeys = Object.keys(global).sort();
                const hasManual = typeof manual !== 'undefined';
                const manualKeys = hasManual ? Object.keys(manual).sort() : [];
                const interfaceString = typeof __interfaces !== 'undefined' ? __interfaces : null;
                const routerInterface = typeof __getToolInterface === 'function' ? __getToolInterface('router_mcp.list_routers') : null;
                return {
                    global_keys: globalKeys,
                    has_manual: hasManual,
                    manual_keys: manualKeys,
                    interfaces_present: interfaceString !== null,
                    interfaces_preview: interfaceString ? interfaceString.slice(0, 400) : null,
                    router_mcp_list_routers_interface: routerInterface
                };
            `;

            const { result, logs } = await client.callToolChain(code, 10000);
            return {
                content: [{
                    type: "text" as const,
                    text: JSON.stringify({ success: true, result, logs }, null, 2)
                }]
            };
        } catch (e: any) {
            return {
                isError: true,
                content: [{ type: "text" as const, text: e.message }]
            };
        }
    });

    // Code Mode specific tools
    mcp.registerTool("call_tool_chain", {
        title: "Execute TypeScript Code with Tool Access",
        description: "Execute TypeScript code with direct access to all registered tools as hierarchical functions (e.g., manual.tool()).",
        inputSchema: {
            code: z.string().describe("TypeScript code to execute with access to all registered tools."),
            timeout: z.number().optional().default(30000).describe("Optional timeout in milliseconds (default: 30000)."),
            max_output_size: z.number().optional().default(200000).describe("Optional maximum output size in characters (default: 200000)."),
        } as any,
    }, async (input: any, _extra: any) => {
        const client = await initializeUtcpClient();
        try {
            console.log(`[code-mode-mcp] call_tool_chain invoked; preparing sandbox tool list.`);
        const preTools = await client.config.tool_repository.getTools();
        console.log(`[code-mode-mcp] client has ${preTools.length} registered UTCP tools: ${preTools.map((t) => t.name).join(', ')}`);

        const { result, logs } = await client.callToolChain(input.code, input.timeout);
            
            function truncateText(text: string): string {
                if (text.length <= input.max_output_size) {
                    return text;
                }
                return text.slice(0, input.max_output_size) + "...\nmax_output_size exceeded";
            }

            let content: Array<ContentBlock> = new Array<ContentBlock>();
            let processedResult: Array<any> = new Array<any>();

            // Handle MCP response content blocks
            // Based on logic from McpCommunicationProtocol._processMcpToolResult

            let mcpContentFound = false;
            // Case 1: content blocks passed as an array (when more than one)
            if (Array.isArray(result)) {
                for (const item of result) {
                    if (ContentBlockSchema.safeParse(item).success) {
                        content.push(item as ContentBlock);
                        mcpContentFound = true;
                    } else {
                        // Text blocks are returned as plain object or string
                        processedResult.push(item);
                    }
                }
            // Case 2: when a single content block is returned, it passed directly
            } else if (ContentBlockSchema.safeParse(result).success) {
                content.push(result as ContentBlock);
                mcpContentFound = true;
            // Case 3: result is not a content block - it's either text or structured data or not MCP content at all
            } else {
                processedResult.push(result);
            }
            
            const plainContent: any = processedResult.length > 1 ? processedResult : processedResult[0];
            const jsonContent: string = JSON.stringify({ success: true, nonMcpContentResults: plainContent, logs });
            content.push({ type: "text" as const, text: truncateText(jsonContent) });

            return { content: content };
        } catch (e: any) {
            return {
                isError: true,
                content: [{ type: "text" as const, text: e.message }]
            };
        }
    });

}

async function initializeUtcpClient(): Promise<CodeModeUtcpClient> {
    if (utcpClient) {
        return utcpClient;
    }

    // Look for config file: 1) Environment variable, 2) Current working directory, 3) Package directory
    const cwd = process.cwd();
    const packageDir = __dirname;
    
    let configPath: string;
    let scriptDir: string;
    
    // Check if UTCP_CONFIG_FILE environment variable is set
    if (process.env.UTCP_CONFIG_FILE) {
        configPath = path.resolve(process.env.UTCP_CONFIG_FILE);
        scriptDir = path.dirname(configPath);
        
        try {
            await fs.access(configPath);
        } catch {
            console.warn(`UTCP config file specified in UTCP_CONFIG_FILE not found: ${configPath}`);
        }
    } else {
        // Fall back to current working directory first, then package directory, then repo root.
        configPath = path.resolve(cwd, '.utcp_config.json');
        scriptDir = cwd;
        try {
            await fs.access(configPath);
        } catch {
            const distConfigPath = path.resolve(packageDir, '.utcp_config.json');
            const repoRootConfigPath = path.resolve(packageDir, '..', '.utcp_config.json');
            try {
                await fs.access(distConfigPath);
                configPath = distConfigPath;
                scriptDir = packageDir;
            } catch {
                try {
                    await fs.access(repoRootConfigPath);
                    configPath = repoRootConfigPath;
                    scriptDir = path.resolve(packageDir, '..');
                } catch {
                    configPath = path.resolve(packageDir, '.utcp_config.json');
                    scriptDir = packageDir;
                }
            }
        }
    }

    console.log(`UTCP bridge starting with configPath=${configPath} scriptDir=${scriptDir}`);

    let rawConfig: any = {};
    try {
        const configFileContent = await fs.readFile(configPath, 'utf-8');
        rawConfig = JSON.parse(configFileContent);
    } catch (e: any) {
        if (e.code !== 'ENOENT') {
            console.warn(`Could not read or parse .utcp_config.json. Error: ${e.message}`);
        }
    }

    if (!rawConfig || typeof rawConfig !== 'object') {
        rawConfig = {};
    }

    if (!Array.isArray(rawConfig.manual_call_templates)) {
        rawConfig.manual_call_templates = [];
    }

    const selfManualDisabled = process.env.UTCP_CODE_MODE_SELF_MANUAL_DISABLED === '1';
    const selfManualName = 'utcp_codemode';
    const selfMcpServerName = 'self';

    if (!selfManualDisabled && !rawConfig.manual_call_templates.some((manual: any) => manual?.name === selfManualName)) {
        rawConfig.manual_call_templates.unshift({
            name: selfManualName,
            call_template_type: 'mcp',
            config: {
                mcpServers: {
                    [selfMcpServerName]: {
                        transport: 'stdio',
                        command: process.execPath,
                        args: [path.resolve(scriptDir, 'dist', 'index.js')],
                        cwd: scriptDir,
                        env: {
                            ...process.env,
                            UTCP_CODE_MODE_SELF_MANUAL_DISABLED: '1'
                        },
                        timeout: 30,
                    }
                }
            },
            allowed_communication_protocols: ['mcp'],
        });
        console.log(`[code-mode-mcp] Auto-registering self MCP manual '${selfManualName}' via ${process.execPath} ${path.resolve(scriptDir, 'dist', 'index.js')}`);
    }

    console.log(`UTCP manual templates final: ${JSON.stringify(rawConfig.manual_call_templates.map((m: any) => m?.name))}`);

    const clientConfig = new UtcpClientConfigSerializer().validateDict(rawConfig);
    console.log(`Using UTCP config at ${configPath}`);
    console.log(`Configured manuals: ${JSON.stringify(rawConfig.manual_call_templates.map((m: any) => m?.name))}`);

    const newClient = await CodeModeUtcpClient.create(scriptDir, clientConfig);
    const loadedTools = await newClient.getTools();
    console.log(`Loaded tools from UTCP repository: ${loadedTools.map((t: any) => t.name).join(', ')}`);

    utcpClient = newClient;
    return utcpClient;
}

main().catch(err => {
    console.error("Failed to start UTCP-MCP Bridge:", err);
    process.exit(1);
});
