const KEY = 'count'

const json = (
	data: unknown,
	status = 200,
	headers: Record<string, string> = {},
) => Response.json(data, {
	status,
	headers: { 'Cache-Control': 'no-store', ...headers },
})

const readCount = async (env: Env) => {
	const value = await env.COUNTER_KV.get(KEY)
	const count = Number(value ?? '0')

	if (!Number.isSafeInteger(count) || count < 0) {
		throw new Error('Stored count is not a non-negative safe integer')
	}

	return count
}

const counter = async (request: Request, env: Env) => {
	if (request.method === 'GET') {
		return json({ count: await readCount(env) })
	}

	if (request.method === 'DELETE') {
		await env.COUNTER_KV.delete(KEY)
		return json({ count: 0 })
	}

	if (request.method !== 'POST') {
		return json({ error: '지원하지 않는 요청입니다.' }, 405, {
			Allow: 'GET, POST, DELETE',
		})
	}

	// Learning example: this read + write is NOT atomic.
	// Concurrent callers can overwrite an increment. Use DO storage for
	// a shared counter that must count simultaneous increments correctly.
	const current = await readCount(env)

	if (current === Number.MAX_SAFE_INTEGER) {
		return json({ error: '카운터의 최대값에 도달했습니다.' }, 409)
	}

	const count = current + 1
	await env.COUNTER_KV.put(KEY, String(count))
	return json({ count })
}

const fetch = async (request: Request, env: Env) => {
	const { pathname } = new URL(request.url)

	if (pathname === '/api/counter') {
		return counter(request, env).catch(error => {
			console.error('Counter request failed', error)
			return json({ error: '처리 결과를 확인하지 못했습니다. 새로고침으로 현재 값을 확인하세요.' }, 500)
		})
	}

	if (pathname === '/api' || pathname.startsWith('/api/')) {
		return json({ error: 'API를 찾을 수 없습니다.' }, 404)
	}

	return env.ASSETS.fetch(request)
}

export default { fetch }
