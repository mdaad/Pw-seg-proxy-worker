import Fastify from 'fastify';
import cors from '@fastify/cors';
import compress from '@fastify/compress';
import { LRUCache } from 'lru-cache';
import { Agent, request as undiciRequest } from 'undici';

// ═══════════════════════════════════════════════
// CONFIG
// ═══════════════════════════════════════════════
const PORT = process.env.PORT || 3000;
const CACHE_TTL_SEGMENT = 30 * 60 * 1000;  // 30 min
const CACHE_TTL_MANIFEST = 60 * 1000;       // 1 min
const MAX_SEGMENT_CACHE = 500;               // 500 segments
const MAX_MANIFEST_CACHE = 100;              // 100 manifests
const PREFETCH_COUNT = 8;                    // Prefetch next 8 segments
const REQUEST_TIMEOUT = 30000;               // 30 sec

// ═══════════════════════════════════════════════
// HIGH-PERFORMANCE HTTP AGENT (Connection Pool)
// ═══════════════════════════════════════════════
const httpAgent = new Agent({
  connections: 200,           // 200 concurrent connections per origin
  pipelining: 10,             // 10 pipelined requests
  keepAliveTimeout: 60000,    // Keep alive 60s
  keepAliveMaxTimeout: 300000,
  connect: { timeout: 10000 },
  bodyTimeout: REQUEST_TIMEOUT,
  headersTimeout: 10000,
});

// ═══════════════════════════════════════════════
// LRU CACHES
// ═══════════════════════════════════════════════
const segmentCache = new LRUCache({
  max: MAX_SEGMENT_CACHE,
  ttl: CACHE_TTL_SEGMENT,
  maxSize: 500 * 1024 * 1024,  // 500MB max
  sizeCalculation: (v) => v?.byteLength || 1024,
  updateAgeOnGet: false,       // Don't reset TTL on hit
});

const manifestCache = new LRUCache({
  max: MAX_MANIFEST_CACHE,
  ttl: CACHE_TTL_MANIFEST,
});

// Track in-flight requests to avoid duplicates
const inFlightRequests = new Map();

// Prefetch tracker (avoid duplicate prefetches)
const prefetchedUrls = new LRUCache({
  max: 1000,
  ttl: 5 * 60 * 1000,  // 5 min
});

// Stats
const stats = {
  segments: { hits: 0, misses: 0, prefetches: 0 },
  manifests: { hits: 0, misses: 0 },
  errors: 0,
};

// ═══════════════════════════════════════════════
// UTILITIES
// ═══════════════════════════════════════════════
const CORS_HEADERS = {
  'access-control-allow-origin': '*',
  'access-control-allow-methods': 'GET, HEAD, OPTIONS',
  'access-control-allow-headers': '*',
  'access-control-expose-headers': 'Content-Length, Content-Range, Content-Type, X-Signature-Expired, X-Cache',
};

const ORIGIN_HEADERS = {
  'user-agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36',
  'referer': 'https://www.pw.live/',
  'origin': 'https://www.pw.live',
  'accept': '*/*',
  'accept-encoding': 'identity',
  'connection': 'keep-alive',
};

function isSegment(url) {
  return /\.(ts|m4s|mp4|aac|webm|cmfv|cmfa)(\?|$)/i.test(url) ||
         /\/(init|segment)/i.test(url);
}

function isManifest(url) {
  return /\.(m3u8|mpd)(\?|$)/i.test(url);
}

function guessContentType(url) {
  const u = url.toLowerCase();
  if (u.includes('.m4s') || u.includes('init')) return 'video/mp4';
  if (u.includes('.mp4')) return 'video/mp4';
  if (u.includes('.ts')) return 'video/MP2T';
  if (u.includes('.m3u8')) return 'application/x-mpegURL';
  if (u.includes('.mpd')) return 'application/dash+xml';
  if (u.includes('.aac')) return 'audio/aac';
  if (u.includes('.webm')) return 'video/webm';
  return 'application/octet-stream';
}

// ═══════════════════════════════════════════════
// SIGNATURE EXPIRY CHECK
// ═══════════════════════════════════════════════
function isSignatureExpired(url) {
  try {
    const u = new URL(url);
    const params = u.searchParams;

    const expires = params.get('Expires');
    if (expires) {
      const expiresTs = parseInt(expires, 10);
      const nowTs = Math.floor(Date.now() / 1000);
      return (expiresTs - nowTs) < 60;
    }

    const policy = params.get('Policy');
    if (policy) {
      try {
        let b64 = policy.replace(/-/g, '+').replace(/_/g, '=').replace(/~/g, '/');
        b64 += '='.repeat((4 - b64.length % 4) % 4);
        const decoded = Buffer.from(b64, 'base64').toString('utf-8');
        const policyJson = JSON.parse(decoded);
        const expiresTs = policyJson?.Statement?.[0]?.Condition?.DateLessThan?.['AWS:EpochTime'];
        if (expiresTs) {
          const nowTs = Math.floor(Date.now() / 1000);
          return (expiresTs - nowTs) < 60;
        }
      } catch {}
    }
    return false;
  } catch {
    return false;
  }
}

// ═══════════════════════════════════════════════
// FAST ORIGIN FETCH (undici - fastest HTTP client)
// ═══════════════════════════════════════════════
async function fetchOrigin(url, rangeHeader = null) {
  const headers = { ...ORIGIN_HEADERS };
  if (rangeHeader) headers['range'] = rangeHeader;

  const { statusCode, headers: respHeaders, body } = await undiciRequest(url, {
    method: 'GET',
    headers,
    dispatcher: httpAgent,
    bodyTimeout: REQUEST_TIMEOUT,
    headersTimeout: 10000,
  });

  return { statusCode, headers: respHeaders, body };
}

// ═══════════════════════════════════════════════
// SEGMENT PREDICTOR (predict next N segments)
// ═══════════════════════════════════════════════
function predictNextSegments(currentUrl, count = 8) {
  try {
    const parsed = new URL(currentUrl);
    const path = parsed.pathname;
    const query = parsed.search;

    // Pattern 1: number in filename (e.g., segment_001.ts)
    const match = path.match(/^(.+?)(\d+)(\.[^.]+)$/);
    if (match) {
      const [, prefix, numStr, ext] = match;
      const num = parseInt(numStr, 10);
      const padLen = numStr.length;
      const results = [];
      for (let i = 1; i <= count; i++) {
        const nextNum = String(num + i).padStart(padLen, '0');
        results.push(`${parsed.origin}${prefix}${nextNum}${ext}${query}`);
      }
      return results;
    }

    // Pattern 2: number in path segment
    const parts = path.split('/');
    for (let i = parts.length - 2; i >= 1; i--) {
      if (/^\d+$/.test(parts[i])) {
        const num = parseInt(parts[i], 10);
        const padLen = parts[i].length;
        const results = [];
        for (let j = 1; j <= count; j++) {
          const newParts = [...parts];
          newParts[i] = String(num + j).padStart(padLen, '0');
          results.push(`${parsed.origin}${newParts.join('/')}${query}`);
        }
        return results;
      }
    }

    return [];
  } catch {
    return [];
  }
}

// ═══════════════════════════════════════════════
// STREAM TO BUFFER (fast collect)
// ═══════════════════════════════════════════════
async function streamToBuffer(stream) {
  const chunks = [];
  let totalLength = 0;
  for await (const chunk of stream) {
    chunks.push(chunk);
    totalLength += chunk.length;
  }
  return Buffer.concat(chunks, totalLength);
}

// ═══════════════════════════════════════════════
// PREFETCH SEGMENTS (background)
// ═══════════════════════════════════════════════
async function prefetchSegments(urls) {
  const toFetch = urls.filter(url => {
    if (prefetchedUrls.has(url)) return false;
    if (segmentCache.has(url)) return false;
    if (isSignatureExpired(url)) return false;
    return true;
  });

  if (toFetch.length === 0) return;

  // Fire and forget - parallel prefetch
  Promise.allSettled(
    toFetch.map(async (url) => {
      try {
        prefetchedUrls.set(url, true);
        
        const { statusCode, body } = await fetchOrigin(url);
        if (statusCode !== 200) return;

        const buffer = await streamToBuffer(body);
        
        // Store in cache
        segmentCache.set(url, buffer);
        stats.segments.prefetches++;
      } catch (err) {
        // Silent fail
      }
    })
  ).catch(() => {});
}

// ═══════════════════════════════════════════════
// FASTIFY SERVER SETUP
// ═══════════════════════════════════════════════
const fastify = Fastify({
  logger: {
    level: 'warn',  // Only warnings and errors
    transport: process.env.NODE_ENV !== 'production' ? {
      target: 'pino-pretty'
    } : undefined,
  },
  bodyLimit: 100 * 1024 * 1024,  // 100MB
  disableRequestLogging: true,     // Faster
  trustProxy: true,
  keepAliveTimeout: 60000,
  connectionTimeout: 30000,
});

// Register plugins
await fastify.register(cors, {
  origin: '*',
  methods: ['GET', 'HEAD', 'OPTIONS'],
  exposedHeaders: ['Content-Length', 'Content-Range', 'Content-Type', 'X-Signature-Expired', 'X-Cache'],
});

await fastify.register(compress, {
  encodings: ['gzip', 'deflate', 'br'],
  threshold: 1024,
});

// ═══════════════════════════════════════════════
// ROUTES
// ═══════════════════════════════════════════════

// Health check
fastify.get('/health', async (req, reply) => {
  return {
    status: 'ok',
    service: 'delta-verse-proxy',
    uptime: process.uptime(),
    memory: {
      rss: Math.round(process.memoryUsage().rss / 1024 / 1024) + ' MB',
      heap: Math.round(process.memoryUsage().heapUsed / 1024 / 1024) + ' MB',
    },
    cache: {
      segments: {
        size: segmentCache.size,
        max: MAX_SEGMENT_CACHE,
        hits: stats.segments.hits,
        misses: stats.segments.misses,
        prefetches: stats.segments.prefetches,
        hitRate: stats.segments.hits + stats.segments.misses > 0
          ? ((stats.segments.hits / (stats.segments.hits + stats.segments.misses)) * 100).toFixed(1) + '%'
          : '0%',
      },
      manifests: {
        size: manifestCache.size,
        max: MAX_MANIFEST_CACHE,
        hits: stats.manifests.hits,
        misses: stats.manifests.misses,
      },
    },
    errors: stats.errors,
  };
});

// ═══════════════════════════════════════════════
// MAIN PROXY ROUTE: /cf/<hostname>/<path>
// ═══════════════════════════════════════════════
fastify.route({
  method: ['GET', 'HEAD', 'OPTIONS'],
  url: '/cf/*',
  handler: async (req, reply) => {
    if (req.method === 'OPTIONS') {
      reply.code(204).headers(CORS_HEADERS).send();
      return;
    }

    try {
      // Extract target URL from /cf/hostname/path
      const cfPath = req.url.replace(/^\/cf\//, '');
      if (!cfPath) {
        return reply.code(400).send({ error: 'Invalid path' });
      }

      // Strip prefetch hints
      const urlObj = new URL(`https://${cfPath}`);
      for (let i = 1; i <= 10; i++) {
        urlObj.searchParams.delete(`__n${i}`);
      }
      const targetUrl = urlObj.toString();

      // ✅ SIGNATURE EXPIRY CHECK
      if (isSignatureExpired(targetUrl)) {
        reply.code(410)
          .headers({
            ...CORS_HEADERS,
            'x-signature-expired': 'true',
          })
          .send('Signature expired');
        return;
      }

      // Handle HEAD requests
      if (req.method === 'HEAD') {
        try {
          const { statusCode, headers } = await fetchOrigin(targetUrl);
          reply.code(statusCode).headers({
            ...CORS_HEADERS,
            'content-type': headers['content-type'] || guessContentType(targetUrl),
            'content-length': headers['content-length'] || '',
            'accept-ranges': 'bytes',
          }).send();
          return;
        } catch (err) {
          reply.code(200).headers(CORS_HEADERS).send();
          return;
        }
      }

      const rangeHeader = req.headers.range;

      // ══════ MANIFEST HANDLING ══════
      if (isManifest(targetUrl)) {
        const cacheKey = targetUrl;
        const cached = manifestCache.get(cacheKey);

        if (cached) {
          stats.manifests.hits++;
          reply.headers({
            ...CORS_HEADERS,
            'content-type': cached.contentType,
            'cache-control': 'public, max-age=30',
            'x-cache': 'HIT',
          }).send(cached.data);
          return;
        }

        stats.manifests.misses++;

        const { statusCode, headers, body } = await fetchOrigin(targetUrl);

        if (statusCode === 403) {
          reply.code(410)
            .headers({
              ...CORS_HEADERS,
              'x-signature-expired': 'true',
            })
            .send('Manifest signature expired');
          return;
        }

        if (statusCode !== 200) {
          const buffer = await streamToBuffer(body);
          reply.code(statusCode).headers(CORS_HEADERS).send(buffer);
          return;
        }

        const buffer = await streamToBuffer(body);
        const contentType = headers['content-type'] || guessContentType(targetUrl);

        // Cache manifest
        manifestCache.set(cacheKey, {
          data: buffer,
          contentType,
        });

        reply.headers({
          ...CORS_HEADERS,
          'content-type': contentType,
          'cache-control': 'public, max-age=30',
          'x-cache': 'MISS',
        }).send(buffer);
        return;
      }

      // ══════ SEGMENT HANDLING ══════
      if (isSegment(targetUrl)) {
        // Check cache
        const cached = segmentCache.get(targetUrl);
        if (cached) {
          stats.segments.hits++;

          // Fire prefetch for next segments (non-blocking)
          const nextUrls = predictNextSegments(targetUrl, PREFETCH_COUNT);
          if (nextUrls.length > 0) {
            setImmediate(() => prefetchSegments(nextUrls));
          }

          // Handle range from cache
          if (rangeHeader) {
            const match = rangeHeader.match(/bytes=(\d+)-(\d*)/);
            if (match) {
              const total = cached.length;
              const start = parseInt(match[1], 10);
              const end = match[2] ? parseInt(match[2], 10) : total - 1;
              const chunkSize = end - start + 1;

              reply.code(206).headers({
                ...CORS_HEADERS,
                'content-type': guessContentType(targetUrl),
                'content-length': String(chunkSize),
                'content-range': `bytes ${start}-${end}/${total}`,
                'accept-ranges': 'bytes',
                'cache-control': 'public, max-age=86400',
                'x-cache': 'HIT',
              }).send(cached.slice(start, end + 1));
              return;
            }
          }

          reply.headers({
            ...CORS_HEADERS,
            'content-type': guessContentType(targetUrl),
            'content-length': String(cached.length),
            'accept-ranges': 'bytes',
            'cache-control': 'public, max-age=86400',
            'x-cache': 'HIT',
          }).send(cached);
          return;
        }

        stats.segments.misses++;

        // Deduplicate in-flight requests
        if (inFlightRequests.has(targetUrl)) {
          try {
            const buffer = await inFlightRequests.get(targetUrl);
            reply.headers({
              ...CORS_HEADERS,
              'content-type': guessContentType(targetUrl),
              'content-length': String(buffer.length),
              'accept-ranges': 'bytes',
              'cache-control': 'public, max-age=86400',
              'x-cache': 'COALESCED',
            }).send(buffer);
            return;
          } catch (err) {
            inFlightRequests.delete(targetUrl);
          }
        }

        // Fetch from origin
        const fetchPromise = (async () => {
          const { statusCode, headers, body } = await fetchOrigin(targetUrl, rangeHeader);

          if (statusCode === 403) {
            throw { code: 410, expired: true };
          }

          if (statusCode !== 200 && statusCode !== 206) {
            throw { code: statusCode };
          }

          const buffer = await streamToBuffer(body);
          
          // Cache only if full response and reasonable size
          if (statusCode === 200 && buffer.length < 10 * 1024 * 1024) {
            segmentCache.set(targetUrl, buffer);
          }

          return { buffer, headers, statusCode };
        })();

        inFlightRequests.set(targetUrl, fetchPromise.then(r => r.buffer));

        try {
          const { buffer, headers, statusCode } = await fetchPromise;

          // Fire prefetch (non-blocking)
          const nextUrls = predictNextSegments(targetUrl, PREFETCH_COUNT);
          if (nextUrls.length > 0) {
            setImmediate(() => prefetchSegments(nextUrls));
          }

          const respHeaders = {
            ...CORS_HEADERS,
            'content-type': headers['content-type'] || guessContentType(targetUrl),
            'content-length': String(buffer.length),
            'accept-ranges': 'bytes',
            'cache-control': 'public, max-age=86400',
            'x-cache': 'MISS',
          };

          if (headers['content-range']) {
            respHeaders['content-range'] = headers['content-range'];
          }

          reply.code(statusCode).headers(respHeaders).send(buffer);
        } catch (err) {
          if (err.expired) {
            reply.code(410).headers({
              ...CORS_HEADERS,
              'x-signature-expired': 'true',
            }).send('Segment expired');
          } else {
            reply.code(err.code || 500).headers(CORS_HEADERS).send('Origin error');
          }
        } finally {
          inFlightRequests.delete(targetUrl);
        }
        return;
      }

      // ══════ OTHER (keys, etc) ══════
      const { statusCode, headers, body } = await fetchOrigin(targetUrl, rangeHeader);

      if (statusCode === 403) {
        reply.code(410).headers({
          ...CORS_HEADERS,
          'x-signature-expired': 'true',
        }).send('Access denied');
        return;
      }

      const buffer = await streamToBuffer(body);
      reply.code(statusCode).headers({
        ...CORS_HEADERS,
        'content-type': headers['content-type'] || guessContentType(targetUrl),
      }).send(buffer);

    } catch (err) {
      stats.errors++;
      fastify.log.error(err);
      reply.code(500).headers(CORS_HEADERS).send({ error: err.message });
    }
  }
});

// ═══════════════════════════════════════════════
// LEGACY /proxy ROUTE (backward compatibility)
// ═══════════════════════════════════════════════
fastify.get('/proxy', async (req, reply) => {
  const targetUrl = req.query.url;
  if (!targetUrl) {
    return reply.code(400).send({ error: 'url param required' });
  }

  if (isSignatureExpired(targetUrl)) {
    reply.code(410).headers({
      ...CORS_HEADERS,
      'x-signature-expired': 'true',
    }).send('Signature expired');
    return;
  }

  try {
    const { statusCode, headers, body } = await fetchOrigin(targetUrl, req.headers.range);
    
    if (statusCode === 403) {
      reply.code(410).headers({
        ...CORS_HEADERS,
        'x-signature-expired': 'true',
      }).send('Expired');
      return;
    }

    const buffer = await streamToBuffer(body);
    reply.code(statusCode).headers({
      ...CORS_HEADERS,
      'content-type': headers['content-type'] || guessContentType(targetUrl),
      'content-length': String(buffer.length),
    }).send(buffer);
  } catch (err) {
    reply.code(500).headers(CORS_HEADERS).send('Fetch error');
  }
});

// Root
fastify.get('/', async (req, reply) => {
  return {
    service: 'Delta Verse Proxy',
    version: '1.0.0',
    endpoints: [
      '/health',
      '/cf/<hostname>/<path>',
      '/proxy?url=<url>',
    ],
  };
});

// ═══════════════════════════════════════════════
// GRACEFUL SHUTDOWN
// ═══════════════════════════════════════════════
const shutdown = async () => {
  console.log('\n🛑 Shutting down gracefully...');
  await fastify.close();
  await httpAgent.close();
  process.exit(0);
};

process.on('SIGTERM', shutdown);
process.on('SIGINT', shutdown);

// ═══════════════════════════════════════════════
// START SERVER
// ═══════════════════════════════════════════════
try {
  await fastify.listen({ 
    port: PORT, 
    host: '0.0.0.0',
    backlog: 511,
  });
  console.log(`🚀 Delta Verse Proxy running on port ${PORT}`);
  console.log(`📊 Health: http://localhost:${PORT}/health`);
} catch (err) {
  console.error('Server failed to start:', err);
  process.exit(1);
}