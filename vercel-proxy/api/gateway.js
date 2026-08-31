// Stable public entrypoint for the local Codex Session Bridge.
//
// ChatGPT talks to a fixed Vercel production URL; this edge function forwards
// to whatever ephemeral tunnel origin the Mac currently has. When the tunnel
// URL changes (reboot, tunnel restart), only BRIDGE_TARGET is updated here -
// the ChatGPT connector keeps working untouched.
//
//   ChatGPT -> https://<project>.vercel.app/p/<PROXY_SECRET>/mcp
//           -> <BRIDGE_TARGET>/mcp   with  Authorization: Bearer <BRIDGE_TOKEN>
//
// The bridge's own bearer token never appears in the public URL: callers
// present PROXY_SECRET, and the real token is injected server-side from an
// encrypted env var. The two rotate independently.
//
// This is not an open proxy: the upstream origin is fixed by BRIDGE_TARGET and
// only the path suffix after /p/<secret>/ is passed through.

export const config = { runtime: 'edge' }

function unauthorized() {
  return new Response(JSON.stringify({ error: 'unauthorized' }), {
    status: 401,
    headers: { 'content-type': 'application/json' },
  })
}

// Constant-time-ish compare so the secret can't be probed byte by byte.
function safeEqual(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string') return false
  if (a.length !== b.length) return false
  let diff = 0
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i)
  return diff === 0
}

export default async function handler(req) {
  const target = process.env.BRIDGE_TARGET
  const token = process.env.BRIDGE_TOKEN
  const secret = process.env.PROXY_SECRET

  if (!target || !token || !secret) {
    return new Response(
      JSON.stringify({ error: 'gateway_not_configured' }),
      { status: 503, headers: { 'content-type': 'application/json' } },
    )
  }

  const url = new URL(req.url)
  // Expect /p/<secret>/<rest...>
  const parts = url.pathname.split('/').filter(Boolean)
  if (parts.length < 2 || parts[0] !== 'p' || !safeEqual(parts[1], secret)) {
    return unauthorized()
  }
  const rest = parts.slice(2).join('/')
  const upstream = `${target.replace(/\/$/, '')}/${rest}${url.search}`

  const headers = new Headers(req.headers)
  headers.delete('host')
  headers.delete('connection')
  headers.set('authorization', `Bearer ${token}`)

  let res
  try {
    res = await fetch(upstream, {
      method: req.method,
      headers,
      body: ['GET', 'HEAD'].includes(req.method) ? undefined : req.body,
      redirect: 'manual',
      // Required by the edge runtime when streaming a request body through.
      duplex: 'half',
    })
  } catch (e) {
    // The Mac is asleep, the tunnel died, or the bridge is down.
    return new Response(
      JSON.stringify({ error: 'bridge_unreachable', detail: String(e).slice(0, 200) }),
      { status: 502, headers: { 'content-type': 'application/json' } },
    )
  }

  // Stream the response straight through - MCP streamable HTTP replies with
  // text/event-stream and must not be buffered.
  const outHeaders = new Headers(res.headers)
  outHeaders.delete('content-encoding')
  outHeaders.delete('content-length')
  return new Response(res.body, { status: res.status, headers: outHeaders })
}
