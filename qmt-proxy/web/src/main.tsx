import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { App as AntdApp, ConfigProvider } from 'antd'
import App from './App'
import './index.css'
import 'antd/dist/reset.css'

const theme = {
  token: {
    colorPrimary: '#0f766e',
    colorInfo: '#0f766e',
    colorSuccess: '#0b8f67',
    colorWarning: '#cb7a00',
    colorError: '#c03a2b',
    colorBgBase: '#f8f3e7',
    colorTextBase: '#1d2a32',
    borderRadius: 18,
    fontFamily:
      '"Avenir Next", "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif',
  },
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ConfigProvider theme={theme}>
      <AntdApp>
        <App />
      </AntdApp>
    </ConfigProvider>
  </StrictMode>,
)
