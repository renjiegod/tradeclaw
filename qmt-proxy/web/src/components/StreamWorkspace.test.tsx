import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { renderWithProviders } from '../test/render'
import { StreamWorkspace } from './StreamWorkspace'
import type {
  ConnectionConfig,
  SubscriptionInfo,
} from '../types'

class MockWebSocket {
  static instances: MockWebSocket[] = []

  url: string
  readyState = 0
  sent: string[] = []
  onopen: ((event: Event) => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  onerror: ((event: Event) => void) | null = null
  onclose: ((event: CloseEvent) => void) | null = null

  constructor(url: string) {
    this.url = url
    MockWebSocket.instances.push(this)
  }

  send(data: string) {
    this.sent.push(data)
  }

  close() {
    this.readyState = 3
    this.onclose?.({} as CloseEvent)
  }

  open() {
    this.readyState = 1
    this.onopen?.(new Event('open'))
  }

  emit(payload: unknown) {
    const data = typeof payload === 'string' ? payload : JSON.stringify(payload)
    this.onmessage?.({ data } as MessageEvent)
  }
}

const config: ConnectionConfig = {
  baseUrl: 'http://127.0.0.1:8000',
  apiKey: 'your-api-key',
}

const subscriptions: SubscriptionInfo[] = [
  {
    subscription_id: 'sub_live',
    symbols: ['000001.SZ'],
    period: 'tick',
    start_date: '',
    adjust_type: 'none',
    subscription_type: 'quote',
    created_at: '2025-01-01T09:30:00',
    last_heartbeat: '2025-01-01T09:31:00',
    active: true,
    queue_size: 0,
    subids_xtquant: [],
  },
]

describe('StreamWorkspace', () => {
  it('connects to the websocket and keeps a bounded message history', async () => {
    const user = userEvent.setup()
    vi.stubGlobal('WebSocket', MockWebSocket as unknown as typeof WebSocket)

    renderWithProviders(
      <StreamWorkspace
        config={config}
        subscriptions={subscriptions}
        selectedSubscriptionId="sub_live"
        onSelectedSubscriptionChange={vi.fn()}
      />,
    )

    await user.click(screen.getByRole('button', { name: '开始连接' }))

    const socket = MockWebSocket.instances[0]
    expect(socket.url).toBe('ws://127.0.0.1:8000/ws/quote/sub_live')

    socket.open()
    socket.emit({
      type: 'connected',
      subscription_id: 'sub_live',
      timestamp: '2025-01-01T09:30:00',
    })
    socket.emit({ type: 'pong', timestamp: '2025-01-01T09:30:01' })

    for (let index = 0; index < 205; index += 1) {
      socket.emit({
        type: 'quote',
        timestamp: `2025-01-01T09:30:${String(index % 60).padStart(2, '0')}`,
        data: {
          seq: index,
          stock_code: '000001.SZ',
        },
      })
    }

    expect(await screen.findByText('connected')).toBeInTheDocument()
    expect(screen.getByText('200')).toBeInTheDocument()
    expect(screen.getAllByText(/"seq": 204/).length).toBeGreaterThan(0)
    expect(screen.queryAllByText(/"seq": 0/)).toHaveLength(0)
  })
})
