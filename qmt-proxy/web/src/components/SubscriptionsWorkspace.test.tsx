import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { renderWithProviders } from '../test/render'
import { SubscriptionsWorkspace } from './SubscriptionsWorkspace'
import {
  DEFAULT_SUBSCRIPTION_FORM,
  type ConnectionConfig,
  type SubscriptionInfo,
} from '../types'

function jsonResponse(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      'Content-Type': 'application/json',
    },
  })
}

const config: ConnectionConfig = {
  baseUrl: 'http://127.0.0.1:8000',
  apiKey: 'your-api-key',
}

function buildSubscription(
  subscriptionId: string,
  overrides: Partial<SubscriptionInfo> = {},
): SubscriptionInfo {
  return {
    subscription_id: subscriptionId,
    symbols: ['000001.SZ'],
    period: 'tick',
    start_date: '',
    adjust_type: 'none',
    subscription_type: 'quote',
    created_at: '2025-01-01T09:30:00',
    last_heartbeat: '2025-01-01T09:31:00',
    active: true,
    queue_size: 2,
    subids_xtquant: [],
    ...overrides,
  }
}

describe('SubscriptionsWorkspace', () => {
  it('loads, creates, inspects, and deletes subscriptions', async () => {
    const user = userEvent.setup()
    const existing = buildSubscription('sub_existing')
    const created = buildSubscription('sub_new', {
      symbols: ['000001.SZ', '600000.SH'],
      queue_size: 0,
    })
    const subscriptions: SubscriptionInfo[] = [existing]
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      const headers = new Headers(init?.headers)

      expect(headers.get('Authorization')).toBe('Bearer your-api-key')

      if (url.endsWith('/api/v1/data/subscriptions') && method === 'GET') {
        return jsonResponse({
          subscriptions,
          total: subscriptions.length,
        })
      }

      if (url.endsWith('/api/v1/data/subscription') && method === 'POST') {
        const body = JSON.parse(String(init?.body)) as Record<string, unknown>
        expect(body).toMatchObject({
          symbols: ['000001.SZ', '600000.SH'],
          period: 'tick',
          adjust_type: 'none',
          subscription_type: 'quote',
        })
        subscriptions.push(created)
        return jsonResponse({
          ...created,
          status: 'active',
          message: '订阅创建成功',
        })
      }

      if (url.endsWith('/api/v1/data/subscription/sub_new') && method === 'GET') {
        return jsonResponse(created)
      }

      if (
        url.endsWith('/api/v1/data/subscription/sub_new') &&
        method === 'DELETE'
      ) {
        subscriptions.splice(
          subscriptions.findIndex(
            (subscription) => subscription.subscription_id === 'sub_new',
          ),
          1,
        )
        return jsonResponse({
          success: true,
          message: '订阅已取消',
          subscription_id: 'sub_new',
        })
      }

      return jsonResponse({ message: 'not found' }, 404)
    })

    vi.stubGlobal('fetch', fetchMock)

    const onOpenStream = vi.fn()
    const onSubscriptionsChange = vi.fn()

    renderWithProviders(
      <SubscriptionsWorkspace
        config={config}
        initialValues={DEFAULT_SUBSCRIPTION_FORM}
        onPersistPreferences={vi.fn()}
        onSubscriptionsChange={onSubscriptionsChange}
        onOpenStream={onOpenStream}
      />,
    )

    expect(await screen.findByText('sub_existing')).toBeInTheDocument()

    await user.clear(screen.getByPlaceholderText('000001.SZ,600000.SH'))
    await user.type(
      screen.getByPlaceholderText('000001.SZ,600000.SH'),
      '000001.SZ,600000.SH',
    )
    await user.click(screen.getByRole('button', { name: '创建订阅' }))

    expect(await screen.findByText('已创建订阅 sub_new')).toBeInTheDocument()
    await waitFor(() => expect(onOpenStream).toHaveBeenCalledWith('sub_new'))

    const createdRow = screen.getByText('sub_new').closest('tr')
    expect(createdRow).not.toBeNull()

    await user.click(
      within(createdRow as HTMLElement).getByRole('button', {
        name: '查看详情',
      }),
    )

    expect(await screen.findByText('订阅详情')).toBeInTheDocument()
    expect(screen.getByText('最近心跳')).toBeInTheDocument()

    await user.click(
      within(createdRow as HTMLElement).getByRole('button', {
        name: '取消订阅',
      }),
    )

    expect(await screen.findByText('订阅已取消')).toBeInTheDocument()
    await waitFor(() => expect(onSubscriptionsChange).toHaveBeenCalled())
    expect(fetchMock).toHaveBeenCalled()
  })
})
