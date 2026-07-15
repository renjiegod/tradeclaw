import { Button, Card, Form, Input, Space, Tag, Typography } from 'antd'
import type { ConnectionConfig } from '../types'
import { defaultBaseUrl, normalizeBaseUrl } from '../lib/storage'

interface ConnectionSettingsProps {
  config: ConnectionConfig
  onSave: (config: ConnectionConfig) => void
}

export function ConnectionSettings({
  config,
  onSave,
}: ConnectionSettingsProps) {
  const [form] = Form.useForm<ConnectionConfig>()

  return (
    <Card
      className="workspace-card connector-card"
      title="连接配置"
      extra={<Tag color={config.apiKey ? 'green' : 'gold'}>{config.apiKey ? 'Bearer 已配置' : '等待 API Key'}</Tag>}
    >
      <Typography.Paragraph className="card-intro">
        前端通过统一的 REST / WebSocket 接入层访问当前 qmt-proxy
        服务。配置会保存在当前浏览器中，后续打开 `/ui` 时自动复用。
      </Typography.Paragraph>

      <Form
        form={form}
        layout="vertical"
        initialValues={config}
        onFinish={(values) =>
          onSave({
            baseUrl: normalizeBaseUrl(values.baseUrl),
            apiKey: values.apiKey.trim(),
          })
        }
      >
        <div className="responsive-grid">
          <Form.Item
            label="Backend URL"
            name="baseUrl"
            rules={[{ required: true, message: '请输入后端服务地址' }]}
          >
            <Input
              data-testid="base-url-input"
              placeholder="http://127.0.0.1:8000"
            />
          </Form.Item>

          <Form.Item
            label="API Key"
            name="apiKey"
            rules={[{ required: true, message: '请输入 Bearer API Key' }]}
          >
            <Input.Password
              data-testid="api-key-input"
              placeholder="your-api-key"
            />
          </Form.Item>
        </div>

        <Space wrap>
          <Button type="primary" htmlType="submit">
            保存连接
          </Button>
          <Button
            type="default"
            onClick={() => form.setFieldsValue({ baseUrl: defaultBaseUrl() })}
          >
            使用当前来源
          </Button>
          <Typography.Text type="secondary">
            请求头会自动附加 `Authorization: Bearer &lt;api-key&gt;`
          </Typography.Text>
        </Space>
      </Form>
    </Card>
  )
}
