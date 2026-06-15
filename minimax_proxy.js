const http = require('http');
const https = require('https');

const PORT = 8077;
const MINIMAX_API_KEY = "sk-cp-igs__iveUWLtKeHw31pjYBYsuMHD-6NpduudKIsjHWFJN2wkNVAWGY98b2401N9z-JV-9CtzMZnYUfa-_ah0Pjn42eJESxkblJhooCLWfTa-J8NTja8gBDg"; 
const TARGET_MODEL = "MiniMax-M3"; 

const server = http.createServer((req, res) => {
  // Handle CORS Preflight
  if (req.method === 'OPTIONS') {
    res.writeHead(204, {
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
      'Access-Control-Allow-Headers': '*'
    });
    res.end();
    return;
  }

  // Handle Model Discovery
  if (req.method === 'GET' && (req.url === '/v1/models' || req.url === '/models')) {
    console.log(`[Proxy] Model discovery requested: ${req.url}`);
    res.writeHead(200, {
      'Content-Type': 'application/json',
      'Access-Control-Allow-Origin': '*'
    });
    res.end(JSON.stringify({
      "models": [
        {
          "id": "claude-sonnet-4-6",
          "displayName": "MiniMax M3"
        }
      ],
      "data": [
        {
          "id": "claude-sonnet-4-6",
          "object": "model",
          "created": 1686935002,
          "owned_by": "custom"
        }
      ]
    }));
    return;
  }

  // Handle Message Creation
  if (req.method === 'POST' && req.url === '/v1/messages') {
    let body = '';
    req.on('data', chunk => { body += chunk; });
    req.on('end', () => {
      try {
        let payload = JSON.parse(body);
        
        console.log(`[Proxy] Intercepted model: ${payload.model} -> Mapping to: ${TARGET_MODEL}`);
        payload.model = TARGET_MODEL;

        const postData = JSON.stringify(payload);
        
        const options = {
          hostname: 'api.minimax.io',
          path: '/anthropic/v1/messages',
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Authorization': `Bearer ${MINIMAX_API_KEY}`,
            'anthropic-version': req.headers['anthropic-version'] || '2023-06-01'
          }
        };

        const proxyReq = https.request(options, (proxyRes) => {
          res.writeHead(proxyRes.statusCode, {
            ...proxyRes.headers,
            'Access-Control-Allow-Origin': '*'
          });
          proxyRes.pipe(res);
        });

        proxyReq.on('error', (e) => {
          console.error('[Proxy Error]', e);
          res.writeHead(500, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: e.message }));
        });

        proxyReq.write(postData);
        proxyReq.end();
      } catch (e) {
        console.error('[JSON Parse Error]', e);
        res.writeHead(400, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'Invalid JSON body' }));
      }
    });
  } else {
    res.writeHead(404);
    res.end();
  }
});

server.listen(PORT, () => {
  console.log(`[Proxy] MiniMax local translation proxy is running on http://localhost:${PORT}`);
});
