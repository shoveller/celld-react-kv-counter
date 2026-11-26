import { useCallback, useEffect, useRef, useState } from 'react'
import './style.css'

type Method = 'GET' | 'POST' | 'DELETE'
type CounterPayload = { count: number }

const numberFormat = new Intl.NumberFormat('ko-KR')

const isCounterPayload = (value: unknown): value is CounterPayload =>
	typeof value === 'object' &&
	value !== null &&
	'count' in value &&
	typeof value.count === 'number' &&
	Number.isSafeInteger(value.count) &&
	value.count >= 0

const readResponse = async (method: Method, signal: AbortSignal) => {
	const response = await fetch('/api/counter', {
		method,
		signal,
		cache: 'no-store',
		headers: { Accept: 'application/json' },
	})
	const data: unknown = await response.json()

	if (!response.ok) {
		const message = typeof data === 'object' && data !== null && 'error' in data
			? String(data.error)
			: `서버가 요청을 처리하지 못했습니다. (${response.status})`
		throw new Error(message)
	}

	if (!isCounterPayload(data)) throw new Error('서버 응답에 올바른 카운터 값이 없습니다.')

	return data.count
}

const App = () => {
	const [count, setCount] = useState<number | null>(null)
	const [busy, setBusy] = useState(true)
	const [error, setError] = useState('')
	const [message, setMessage] = useState('저장된 값을 읽고 있습니다.')
	const lockRef = useRef(false)
	const mountedRef = useRef(false)
	const controllerRef = useRef<AbortController | null>(null)

	const request = useCallback(async (method: Method) => {
		if (lockRef.current) return

		lockRef.current = true
		const controller = new AbortController()
		controllerRef.current = controller
		setBusy(true)
		setError('')
		setMessage(method === 'GET' ? '저장된 값을 읽고 있습니다.' : '변경한 값을 저장하고 있습니다.')

		try {
			const nextCount = await readResponse(method, controller.signal)
			if (!mountedRef.current || controller.signal.aborted) return

			setCount(nextCount)
			setMessage(method === 'GET' ? '서버의 최신 값을 불러왔습니다.' : '서버에 저장했습니다.')
		} catch (cause) {
			if (!mountedRef.current || controller.signal.aborted) return

			const detail = cause instanceof Error ? cause.message : '연결 상태를 확인해 주세요.'
			setError(`${detail} 새로 읽기를 눌러 서버 값을 확인해 주세요.`)
			setMessage(method === 'GET' ? '값을 불러오지 못했습니다.' : '저장 결과를 확인하지 못했습니다. 자동 재시도는 하지 않았습니다.')
		} finally {
			if (mountedRef.current && !controller.signal.aborted) {
				lockRef.current = false
				setBusy(false)
			}
		}
	}, [])

	useEffect(() => {
		mountedRef.current = true
		void request('GET')

		return () => {
			mountedRef.current = false
			controllerRef.current?.abort()
			lockRef.current = false
		}
	}, [request])

	return (
		<main className='page'>
			<header className='page-header'>
				<span className='brand-mark' aria-hidden='true'>c</span>
				<span>celld · 작은 앱부터 시작하기</span>
			</header>

			<section className='counter-card' aria-labelledby='counter-title'>
				<div className='eyebrow'><span className='status-dot' /> REACT + WORKER + KV</div>
				<h1 id='counter-title'>숫자 하나,<br />서버에 차곡차곡.</h1>
				<p className='intro'>버튼을 누르면 카운터를 저장합니다.<br />페이지를 다시 열어도 저장된 값을 불러옵니다.</p>

				<div className='counter-panel' aria-busy={busy}>
					<span className='counter-label'>현재 카운터</span>
					<output className='counter-value' aria-live='polite'>
						{count === null ? '—' : numberFormat.format(count)}
					</output>
					<p className='status-message' role='status'>{message}</p>
				</div>

				<button className='increment-button' type='button' disabled={busy || count === null} onClick={() => void request('POST')}>
					<span aria-hidden='true'>+</span> 1 올리기
				</button>
				<div className='secondary-actions'>
					<button type='button' disabled={busy} onClick={() => void request('GET')}>새로 읽기</button>
					<span aria-hidden='true' />
					<button type='button' disabled={busy || count === null} onClick={() => void request('DELETE')}>0으로 초기화</button>
				</div>

				{error && <p className='error-message' role='alert'>{error}</p>}

				<footer className='card-footer'>
					<strong>KV 동작을 배우는 실습 예제</strong>
					<p>여러 브라우저가 동시에 변경하면 증가분이 유실될 수 있습니다. 다른 화면의 변경은 ‘새로 읽기’로 확인하세요.</p>
				</footer>
			</section>
			<p className='page-footer'>내 Docker에서 실행하는 작은 카운터</p>
		</main>
	)
}

export default App
