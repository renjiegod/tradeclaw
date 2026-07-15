import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { renderWithProviders } from '../test/render'
import { MarketQueryWorkspace } from './MarketQueryWorkspace'
import {
  DEFAULT_MARKET_QUERY_FORM,
  type ConnectionConfig,
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

describe('MarketQueryWorkspace', () => {
  it('renders structured and raw query results', async () => {
    const user = userEvent.setup()
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      const headers = new Headers(init?.headers)
      expect(headers.get('Authorization')).toBe('Bearer your-api-key')

      return jsonResponse([
        {
          stock_code: '000001.SZ',
          data: [
            { time: '20250101', close: 12.3, volume: 100 },
            { time: '20250102', close: 12.8, volume: 120 },
          ],
          fields: ['time', 'close', 'volume'],
          period: '1d',
          start_date: '20250101',
          end_date: '20250102',
        },
      ])
    })

    vi.stubGlobal('fetch', fetchMock)

    renderWithProviders(
      <MarketQueryWorkspace
        config={config}
        initialValues={DEFAULT_MARKET_QUERY_FORM}
        onPersistPreferences={vi.fn()}
      />,
    )

    await user.click(screen.getByRole('button', { name: '执行查询' }))

    expect(await screen.findByText('000001.SZ · 2 行')).toBeInTheDocument()
    await user.click(screen.getByRole('tab', { name: '原始 JSON' }))
    expect(await screen.findByText(/"stock_code": "000001.SZ"/)).toBeInTheDocument()
  })

  it('shows backend error messages when a query fails', async () => {
    const user = userEvent.setup()
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse(
          {
            detail: {
              message: 'API密钥缺失',
            },
          },
          401,
        ),
      ),
    )

    renderWithProviders(
      <MarketQueryWorkspace
        config={config}
        initialValues={DEFAULT_MARKET_QUERY_FORM}
        onPersistPreferences={vi.fn()}
      />,
    )

    await user.click(screen.getByRole('button', { name: '执行查询' }))

    expect(await screen.findByText('API密钥缺失')).toBeInTheDocument()
  })
})
