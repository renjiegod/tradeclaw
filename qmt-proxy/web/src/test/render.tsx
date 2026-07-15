import { render } from '@testing-library/react'
import { App as AntdApp, ConfigProvider } from 'antd'
import type { ReactElement } from 'react'

const theme = {
  token: {
    colorPrimary: '#0f766e',
    borderRadius: 18,
  },
}

export function renderWithProviders(ui: ReactElement) {
  return render(
    <ConfigProvider theme={theme}>
      <AntdApp>{ui}</AntdApp>
    </ConfigProvider>,
  )
}
