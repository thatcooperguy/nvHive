#!/usr/bin/env bun
/**
 * nvHive Channel Plugin for Claude Code
 *
 * A two-way Claude Code Channel that:
 * 1. Pushes nvHive events (cost alerts, council results, provider status) into Claude Code sessions
 * 2. Exposes nvHive tools (ask, council, throwdown) so Claude Code can route queries through nvHive
 * 3. Bridges Telegram/Discord messages → nvHive smart routing → back to user
 *
 * Architecture:
 *   External Events → nvHive FastAPI (port 8000) → this plugin → Claude Code session
 *   Claude Code → this plugin's MCP tools → nvHive API → best model → response
 *
 * Usage:
 *   claude --dangerously-load-development-channels server:nvhive
 *   # or after plugin marketplace approval:
 *   claude --channels plugin:nvhive@claude-plugins-official
 */

import { Server } from '@modelcontextprotocol/sdk/server/index.js'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'
import {
  ListToolsRequestSchema,
  CallToolRequestSchema,
} from '@modelcontextprotocol/sdk/types.js'
import { z } from 'zod'

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

const NVHIVE_API_URL = process.env.NVHIVE_API_URL ?? 'http://localhost:8000'
const NVHIVE_API_KEY = process.env.NVHIVE_API_KEY ?? ''
const POLL_INTERVAL_MS = Number(process.env.NVHIVE_POLL_INTERVAL ?? 10_000) // 10s default
const CHANNEL_PORT = Number(process.env.NVHIVE_CHANNEL_PORT ?? 8789)

// ---------------------------------------------------------------------------
// nvHive API client
// ---------------------------------------------------------------------------

async function nvhiveRequest(path: string, options: RequestInit = {}): Promise<any> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(NVHIVE_API_KEY ? { 'X-Hive-API-Key': NVHIVE_API_KEY } : {}),
  }

  const res = await fetch(`${NVHIVE_API_URL}${path}`, {
    ...options,
    headers: { ...headers, ...(options.headers as Record<string, string> ?? {}) },
  })

  if (!res.ok) {
    const text = await res.text()
    throw new Error(`nvHive API ${path}: ${res.status} ${text}`)
  }

  return res.json()
}

// ---------------------------------------------------------------------------
// MCP Server — Channel + Tools
// ---------------------------------------------------------------------------

const mcp = new Server(
  { name: 'nvhive', version: '0.1.0' },
  {
    capabilities: {
      experimental: {
        'claude/channel': {},               // register as a channel
        'claude/channel/permission': {},    // can relay permission prompts
      },
      tools: {},                            // expose nvHive tools
    },
    instructions: `You have access to nvHive, a multi-LLM orchestration platform with 63 models across 23 providers (25 free).

WHEN TO USE NVHIVE TOOLS:
- Use "nvhive_ask" for queries where cost matters — nvHive routes to the cheapest capable model.
- Use "nvhive_ask" with a specific advisor when you need a particular provider (e.g., "groq" for speed, "ollama" for privacy).
- Use "nvhive_council" when you want consensus from multiple models — great for architecture decisions, code reviews, or anything where multiple perspectives help.
- Use "nvhive_throwdown" for deep analysis — two-pass critique between multiple LLMs.
- Use "nvhive_safe" when handling sensitive data — everything stays local via Ollama.

CHANNEL EVENTS:
Events from nvHive arrive as <channel source="nvhive" event_type="...">. These include:
- cost alerts (budget thresholds reached)
- council completion notifications
- provider health changes (circuit breaker open/close)
React to these by informing the user and suggesting actions.

COST OPTIMIZATION:
nvHive's smart router sends simple queries to free providers (Groq, GitHub Models, LLM7, local Nemotron) and only uses premium models when task complexity requires it. Prefer nvHive tools when cost optimization is relevant.`,
  },
)

// ---------------------------------------------------------------------------
// Tools — Claude Code calls these to route through nvHive
// ---------------------------------------------------------------------------

const TOOLS = [
  {
    name: 'nvhive_ask',
    description:
      'Route a query through nvHive\'s smart router. Picks the best model based on task type, cost, and speed. ' +
      'Supports 63 models across 23 providers — 25 free. Use this instead of direct API calls when cost matters.',
    inputSchema: {
      type: 'object' as const,
      properties: {
        prompt: { type: 'string', description: 'The question or task' },
        advisor: {
          type: 'string',
          description:
            'Specific provider: "groq" (fast+free), "github" (free), "openai", "anthropic", "ollama" (local). Leave empty for smart routing.',
        },
        model: {
          type: 'string',
          description: 'Specific model (e.g., "gpt-4o", "llama-3.1-70b"). Leave empty for auto-selection.',
        },
        mode: {
          type: 'string',
          enum: ['code', 'write', 'research', 'reason', 'chat'],
          description: 'Task mode hint for better routing. Leave empty for auto-detect.',
        },
      },
      required: ['prompt'],
    },
  },
  {
    name: 'nvhive_safe',
    description:
      'Query using only local models via Ollama. Nothing leaves the machine. Use for sensitive code, credentials, or private data.',
    inputSchema: {
      type: 'object' as const,
      properties: {
        prompt: { type: 'string', description: 'The question or task' },
        model: { type: 'string', description: 'Local model name (default: auto-select based on GPU)' },
      },
      required: ['prompt'],
    },
  },
  {
    name: 'nvhive_council',
    description:
      'Convene a council of multiple LLMs to debate and synthesize a consensus answer. ' +
      'Great for architecture decisions, code reviews, and complex analysis. 3+ models collaborate.',
    inputSchema: {
      type: 'object' as const,
      properties: {
        prompt: { type: 'string', description: 'The question to present to the council' },
        num_members: {
          type: 'number',
          description: 'Number of council members (2-10, default 3)',
        },
        cabinet: {
          type: 'string',
          enum: [
            'executive', 'engineering', 'security_review', 'code_review',
            'product', 'data', 'homework_help', 'code_tutor', 'essay_review',
          ],
          description: 'Expert persona preset. Leave empty for auto-generated agents.',
        },
        strategy: {
          type: 'string',
          enum: ['weighted_consensus', 'majority_vote', 'best_of'],
          description: 'Consensus strategy. Default: weighted_consensus.',
        },
      },
      required: ['prompt'],
    },
  },
  {
    name: 'nvhive_throwdown',
    description:
      'Two-pass deep analysis: multiple LLMs analyze independently, then cross-critique each other. ' +
      'Use for high-stakes decisions or when you need thorough multi-perspective analysis.',
    inputSchema: {
      type: 'object' as const,
      properties: {
        prompt: { type: 'string', description: 'The question for deep analysis' },
        cabinet: {
          type: 'string',
          description: 'Expert persona preset. Leave empty for auto-generated.',
        },
      },
      required: ['prompt'],
    },
  },
  {
    name: 'nvhive_status',
    description: 'Get nvHive system status: enabled providers, GPU info, budget, and available models.',
    inputSchema: {
      type: 'object' as const,
      properties: {},
    },
  },
  {
    name: 'nvhive_providers',
    description: 'List all available LLM providers, their health status, and free tier limits.',
    inputSchema: {
      type: 'object' as const,
      properties: {},
    },
  },
]

mcp.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: TOOLS }))

mcp.setRequestHandler(CallToolRequestSchema, async (req) => {
  const { name, arguments: args } = req.params
  const params = (args ?? {}) as Record<string, any>

  try {
    switch (name) {
      case 'nvhive_ask': {
        const body: Record<string, any> = { prompt: params.prompt }
        if (params.advisor) body.provider = params.advisor
        if (params.model) body.model = params.model
        if (params.mode) body.mode = params.mode
        const result = await nvhiveRequest('/v1/query', {
          method: 'POST',
          body: JSON.stringify(body),
        })
        return {
          content: [{
            type: 'text',
            text: formatQueryResponse(result),
          }],
        }
      }

      case 'nvhive_safe': {
        const result = await nvhiveRequest('/v1/query', {
          method: 'POST',
          body: JSON.stringify({
            prompt: params.prompt,
            provider: 'ollama',
            model: params.model || undefined,
          }),
        })
        return {
          content: [{
            type: 'text',
            text: formatQueryResponse(result),
          }],
        }
      }

      case 'nvhive_council': {
        const body: Record<string, any> = {
          prompt: params.prompt,
          num_agents: params.num_members ?? 3,
        }
        if (params.cabinet) body.agent_preset = params.cabinet
        if (params.strategy) body.strategy = params.strategy
        const result = await nvhiveRequest('/v1/council', {
          method: 'POST',
          body: JSON.stringify(body),
        })
        return {
          content: [{
            type: 'text',
            text: formatCouncilResponse(result),
          }],
        }
      }

      case 'nvhive_throwdown': {
        const body: Record<string, any> = {
          prompt: params.prompt,
          strategy: 'throwdown',
        }
        if (params.cabinet) body.agent_preset = params.cabinet
        const result = await nvhiveRequest('/v1/council', {
          method: 'POST',
          body: JSON.stringify(body),
        })
        return {
          content: [{
            type: 'text',
            text: formatCouncilResponse(result),
          }],
        }
      }

      case 'nvhive_status': {
        const info = await nvhiveRequest('/v1/system/info')
        return {
          content: [{
            type: 'text',
            text: formatStatusResponse(info),
          }],
        }
      }

      case 'nvhive_providers': {
        const advisors = await nvhiveRequest('/v1/advisors')
        return {
          content: [{
            type: 'text',
            text: formatProvidersResponse(advisors),
          }],
        }
      }

      default:
        throw new Error(`Unknown tool: ${name}`)
    }
  } catch (err: any) {
    return {
      content: [{
        type: 'text',
        text: `nvHive error: ${err.message}\n\nMake sure nvHive API is running: nvh serve`,
      }],
      isError: true,
    }
  }
})

// ---------------------------------------------------------------------------
// Response formatters
// ---------------------------------------------------------------------------

function formatQueryResponse(result: any): string {
  const parts = [result.content ?? result.response ?? JSON.stringify(result)]
  const meta: string[] = []
  if (result.provider) meta.push(`Provider: ${result.provider}`)
  if (result.model) meta.push(`Model: ${result.model}`)
  if (result.cost_usd != null) meta.push(`Cost: $${Number(result.cost_usd).toFixed(6)}`)
  if (result.latency_ms != null) meta.push(`Latency: ${result.latency_ms}ms`)
  if (result.usage?.total_tokens) meta.push(`Tokens: ${result.usage.total_tokens}`)
  if (meta.length) parts.push(`\n---\n${meta.join(' | ')}`)
  return parts.join('')
}

function formatCouncilResponse(result: any): string {
  const parts: string[] = []

  if (result.synthesis) {
    const content = typeof result.synthesis === 'string'
      ? result.synthesis
      : result.synthesis.content ?? JSON.stringify(result.synthesis)
    parts.push(`## Council Synthesis\n\n${content}`)
  }

  if (result.member_responses) {
    parts.push('\n\n## Individual Responses\n')
    for (const [provider, resp] of Object.entries(result.member_responses as Record<string, any>)) {
      const content = typeof resp === 'string' ? resp : (resp.content ?? JSON.stringify(resp))
      const truncated = content.length > 500 ? content.slice(0, 500) + '...' : content
      parts.push(`\n### ${provider}\n${truncated}`)
    }
  }

  const meta: string[] = []
  if (result.strategy) meta.push(`Strategy: ${result.strategy}`)
  if (result.total_cost_usd != null) meta.push(`Total cost: $${Number(result.total_cost_usd).toFixed(6)}`)
  if (result.total_latency_ms) meta.push(`Latency: ${result.total_latency_ms}ms`)
  if (result.agents_used) meta.push(`Agents: ${result.agents_used.join(', ')}`)
  if (meta.length) parts.push(`\n---\n${meta.join(' | ')}`)

  return parts.join('')
}

function formatStatusResponse(info: any): string {
  const parts = ['## nvHive System Status\n']
  if (info.providers) parts.push(`**Providers:** ${info.providers.enabled ?? '?'} enabled`)
  if (info.gpu) {
    const gpu = info.gpu
    if (gpu.name) parts.push(`**GPU:** ${gpu.name} (${gpu.vram_total_mb ?? '?'}MB VRAM)`)
    else parts.push('**GPU:** None detected')
  }
  if (info.budget) {
    const b = info.budget
    parts.push(`**Budget:** $${b.spent_today_usd?.toFixed(4) ?? '0'} / $${b.daily_limit_usd ?? '∞'} today`)
  }
  return parts.join('\n')
}

function formatProvidersResponse(advisors: any): string {
  if (!Array.isArray(advisors) && advisors.providers) advisors = advisors.providers
  if (!Array.isArray(advisors)) return JSON.stringify(advisors, null, 2)

  const lines = ['## Available Providers\n', '| Provider | Status | Free Tier |', '|----------|--------|-----------|']
  for (const a of advisors) {
    const name = a.name ?? a.provider ?? 'unknown'
    const status = a.healthy ? 'Healthy' : a.enabled ? 'Enabled' : 'Disabled'
    const free = a.free_tier ? 'Yes' : 'No'
    lines.push(`| ${name} | ${status} | ${free} |`)
  }
  return lines.join('\n')
}

// ---------------------------------------------------------------------------
// Permission relay — forward approval prompts to nvHive events/webhook
// ---------------------------------------------------------------------------

const PermissionRequestSchema = z.object({
  method: z.literal('notifications/claude/channel/permission_request'),
  params: z.object({
    request_id: z.string(),
    tool_name: z.string(),
    description: z.string(),
    input_preview: z.string(),
  }),
})

mcp.setNotificationHandler(PermissionRequestSchema, async ({ params }) => {
  // Broadcast to any connected SSE listeners
  broadcast(
    JSON.stringify({
      type: 'permission_request',
      request_id: params.request_id,
      tool_name: params.tool_name,
      description: params.description,
      input_preview: params.input_preview,
    })
  )
})

// ---------------------------------------------------------------------------
// Event polling — watch nvHive for events and push into Claude Code
// ---------------------------------------------------------------------------

let lastBudgetWarned = false

async function pollNvhiveEvents() {
  try {
    // Check budget status
    const budget = await nvhiveRequest('/v1/budget/status').catch(() => null)
    if (budget) {
      const pct = budget.percent_used ?? 0
      if (pct >= 80 && !lastBudgetWarned) {
        lastBudgetWarned = true
        await mcp.notification({
          method: 'notifications/claude/channel',
          params: {
            content: `Budget alert: ${pct.toFixed(0)}% used ($${budget.spent_today_usd?.toFixed(4)} / $${budget.daily_limit_usd}). Consider routing queries through free providers.`,
            meta: { event_type: 'budget_warning', severity: 'high' },
          },
        })
      } else if (pct < 70) {
        lastBudgetWarned = false // reset when budget drops
      }
    }

    // Check provider health
    const advisors = await nvhiveRequest('/v1/advisors').catch(() => null)
    if (advisors) {
      const unhealthy = (Array.isArray(advisors) ? advisors : advisors.providers ?? [])
        .filter((a: any) => a.enabled && !a.healthy)
      if (unhealthy.length > 0) {
        const names = unhealthy.map((a: any) => a.name ?? a.provider).join(', ')
        await mcp.notification({
          method: 'notifications/claude/channel',
          params: {
            content: `Provider health alert: ${names} reporting unhealthy. nvHive will auto-route around them.`,
            meta: { event_type: 'provider_health', severity: 'medium' },
          },
        })
      }
    }
  } catch {
    // nvHive API not running — silently skip
  }
}

// ---------------------------------------------------------------------------
// Inbound HTTP server — receives webhooks from nvHive and external systems
// ---------------------------------------------------------------------------

const sseListeners = new Set<(chunk: string) => void>()

function broadcast(text: string) {
  const chunk = text.split('\n').map((l) => `data: ${l}\n`).join('') + '\n'
  for (const emit of sseListeners) emit(chunk)
}

const PERMISSION_REPLY_RE = /^\s*(y|yes|n|no)\s+([a-km-z]{5})\s*$/i

// ---------------------------------------------------------------------------
// Start everything
// ---------------------------------------------------------------------------

// 1. Connect to Claude Code over stdio
await mcp.connect(new StdioServerTransport())

// 2. Start polling nvHive for events
const poller = setInterval(pollNvhiveEvents, POLL_INTERVAL_MS)
// Initial poll after a short delay (let nvHive API start)
setTimeout(pollNvhiveEvents, 3_000)

// 3. Start the inbound HTTP server for webhooks and SSE
Bun.serve({
  port: CHANNEL_PORT,
  hostname: '127.0.0.1',
  idleTimeout: 0,
  async fetch(req) {
    const url = new URL(req.url)

    // GET /events — SSE stream for watching channel events live
    if (req.method === 'GET' && url.pathname === '/events') {
      const stream = new ReadableStream({
        start(ctrl) {
          ctrl.enqueue(': nvhive channel connected\n\n')
          const emit = (chunk: string) => ctrl.enqueue(chunk)
          sseListeners.add(emit)
          req.signal.addEventListener('abort', () => sseListeners.delete(emit))
        },
      })
      return new Response(stream, {
        headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' },
      })
    }

    // GET /health — simple health check
    if (req.method === 'GET' && url.pathname === '/health') {
      return new Response(JSON.stringify({ status: 'ok', channel: 'nvhive', version: '0.1.0' }), {
        headers: { 'Content-Type': 'application/json' },
      })
    }

    // POST /webhook — receive events from nvHive's webhook system
    if (req.method === 'POST' && url.pathname === '/webhook') {
      const body = await req.json().catch(() => ({}))
      const event = body.event ?? 'unknown'
      const data = body.data ?? body

      await mcp.notification({
        method: 'notifications/claude/channel',
        params: {
          content: typeof data === 'string' ? data : JSON.stringify(data, null, 2),
          meta: {
            event_type: event,
            source: 'nvhive_webhook',
            ...(body.severity ? { severity: body.severity } : {}),
          },
        },
      })
      return new Response('ok')
    }

    // POST /push — push arbitrary messages into Claude Code session
    if (req.method === 'POST' && url.pathname === '/push') {
      const body = await req.text()

      // Check for permission reply format
      const m = PERMISSION_REPLY_RE.exec(body)
      if (m) {
        await mcp.notification({
          method: 'notifications/claude/channel/permission',
          params: {
            request_id: m[2].toLowerCase(),
            behavior: m[1].toLowerCase().startsWith('y') ? 'allow' : 'deny',
          },
        })
        return new Response('verdict recorded')
      }

      // Normal message push
      await mcp.notification({
        method: 'notifications/claude/channel',
        params: {
          content: body,
          meta: { event_type: 'push', source: 'http' },
        },
      })
      return new Response('ok')
    }

    // POST /council-result — push completed council results
    if (req.method === 'POST' && url.pathname === '/council-result') {
      const body = await req.json().catch(() => ({}))
      await mcp.notification({
        method: 'notifications/claude/channel',
        params: {
          content: formatCouncilResponse(body),
          meta: { event_type: 'council_complete', source: 'nvhive' },
        },
      })
      return new Response('ok')
    }

    return new Response('not found', { status: 404 })
  },
})

// Cleanup on exit
process.on('SIGINT', () => {
  clearInterval(poller)
  process.exit(0)
})
process.on('SIGTERM', () => {
  clearInterval(poller)
  process.exit(0)
})
