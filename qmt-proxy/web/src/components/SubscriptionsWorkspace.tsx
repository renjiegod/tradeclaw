import {
  Alert,
  Button,
  Card,
  Descriptions,
  Drawer,
  Form,
  Input,
  Select,
  Space,
  Table,
  Tag,
  Typography,
} from 'antd'
import { useEffect, useState } from 'react'
import type { ColumnsType } from 'antd/es/table'
import {
  createSubscription,
  deleteSubscription,
  getSubscriptionInfo,
  listSubscriptions,
  splitListInput,
  ApiError,
} from '../lib/api'
import { formatJson, formatTimestamp } from '../lib/format'
import {
  ADJUST_TYPE_OPTIONS,
  PERIOD_OPTIONS,
  type ConnectionConfig,
  type SubscriptionFormValues,
  type SubscriptionInfo,
} from '../types'

interface SubscriptionsWorkspaceProps {
  config: ConnectionConfig
  initialValues: SubscriptionFormValues
  onPersistPreferences: (values: SubscriptionFormValues) => void
  onSubscriptionsChange: (subscriptions: SubscriptionInfo[]) => void
  onOpenStream: (subscriptionId: string) => void
}

const columns: ColumnsType<SubscriptionInfo> = [
  {
    title: '订阅 ID',
    dataIndex: 'subscription_id',
    key: 'subscription_id',
    width: 220,
    render: (value: string) => <Typography.Text code>{value}</Typography.Text>,
  },
  {
    title: '标的',
    dataIndex: 'symbols',
    key: 'symbols',
    render: (value: string[]) => value.join(', '),
  },
  {
    title: '周期',
    dataIndex: 'period',
    key: 'period',
    width: 90,
  },
  {
    title: '状态',
    dataIndex: 'active',
    key: 'active',
    width: 110,
    render: (value: boolean) => (
      <Tag color={value ? 'green' : 'default'}>{value ? 'active' : 'inactive'}</Tag>
    ),
  },
  {
    title: '队列积压',
    dataIndex: 'queue_size',
    key: 'queue_size',
    width: 110,
  },
  {
    title: '创建时间',
    dataIndex: 'created_at',
    key: 'created_at',
    width: 190,
    render: (value: string) => formatTimestamp(value),
  },
]

export function SubscriptionsWorkspace({
  config,
  initialValues,
  onPersistPreferences,
  onSubscriptionsChange,
  onOpenStream,
}: SubscriptionsWorkspaceProps) {
  const [form] = Form.useForm<SubscriptionFormValues>()
  const [subscriptions, setSubscriptions] = useState<SubscriptionInfo[]>([])
  const [listError, setListError] = useState<string | null>(null)
  const [actionMessage, setActionMessage] = useState<string | null>(null)
  const [loadingList, setLoadingList] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [detailOpen, setDetailOpen] = useState(false)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detail, setDetail] = useState<SubscriptionInfo | null>(null)

  useEffect(() => {
    form.setFieldsValue(initialValues)
  }, [form, initialValues])

  useEffect(() => {
    if (!config.apiKey) {
      setSubscriptions([])
      setListError('请先在顶部保存有效的 API Key，再加载订阅列表。')
      onSubscriptionsChange([])
      return
    }

    void refreshSubscriptions()
  }, [config.apiKey, config.baseUrl])

  async function refreshSubscriptions() {
    setLoadingList(true)
    setListError(null)

    try {
      const response = await listSubscriptions(config)
      setSubscriptions(response.subscriptions)
      onSubscriptionsChange(response.subscriptions)
    } catch (error) {
      const message =
        error instanceof ApiError ? error.message : '加载订阅列表失败'
      setListError(message)
      setSubscriptions([])
      onSubscriptionsChange([])
    } finally {
      setLoadingList(false)
    }
  }

  async function handleCreate(values: SubscriptionFormValues) {
    setSubmitting(true)
    setActionMessage(null)

    try {
      const payload = {
        symbols: splitListInput(values.symbolsText),
        period: values.period,
        start_date: values.startDate,
        adjust_type: values.adjustType,
        subscription_type: values.subscriptionType,
      }

      const created = await createSubscription(config, payload)
      onPersistPreferences(values)
      setActionMessage(`已创建订阅 ${created.subscription_id}`)
      await refreshSubscriptions()
      onOpenStream(created.subscription_id)
    } catch (error) {
      const message =
        error instanceof ApiError ? error.message : '创建订阅失败'
      setActionMessage(message)
    } finally {
      setSubmitting(false)
    }
  }

  async function handleInspect(record: SubscriptionInfo) {
    setDetailOpen(true)
    setDetailLoading(true)

    try {
      const response = await getSubscriptionInfo(config, record.subscription_id)
      setDetail(response)
    } catch (error) {
      const message =
        error instanceof ApiError ? error.message : '加载订阅详情失败'
      setActionMessage(message)
      setDetail(null)
    } finally {
      setDetailLoading(false)
    }
  }

  async function handleDelete(record: SubscriptionInfo) {
    try {
      const response = await deleteSubscription(config, record.subscription_id)
      setActionMessage(response.message)
      if (detail?.subscription_id === record.subscription_id) {
        setDetailOpen(false)
        setDetail(null)
      }
      await refreshSubscriptions()
    } catch (error) {
      const message =
        error instanceof ApiError ? error.message : '取消订阅失败'
      setActionMessage(message)
    }
  }

  return (
    <div className="workspace-stack">
      <Card className="workspace-card" title="创建订阅">
        <Typography.Paragraph className="card-intro">
          使用现有 REST 接口创建行情订阅。创建成功后，界面会自动刷新列表，并将
          `subscription_id` 带到实时推送面板中。
        </Typography.Paragraph>

        <Form
          form={form}
          layout="vertical"
          initialValues={initialValues}
          onFinish={handleCreate}
        >
          <div className="responsive-grid">
            <Form.Item
              label="股票代码"
              name="symbolsText"
              rules={[{ required: true, message: '请输入股票代码' }]}
            >
              <Input.TextArea
                autoSize={{ minRows: 2, maxRows: 4 }}
                placeholder="000001.SZ,600000.SH"
              />
            </Form.Item>

            <Form.Item label="周期" name="period">
              <Select
                options={PERIOD_OPTIONS.map((value) => ({
                  label: value,
                  value,
                }))}
              />
            </Form.Item>

            <Form.Item label="开始日期" name="startDate">
              <Input placeholder="YYYYMMDD 或 YYYYMMDDHHMMSS" />
            </Form.Item>

            <Form.Item label="复权" name="adjustType">
              <Select
                options={ADJUST_TYPE_OPTIONS.map((value) => ({
                  label: value,
                  value,
                }))}
              />
            </Form.Item>

            <Form.Item label="订阅类型" name="subscriptionType">
              <Select
                options={[{ label: 'quote', value: 'quote' }]}
                disabled
              />
            </Form.Item>
          </div>

          <Space wrap>
            <Button type="primary" htmlType="submit" loading={submitting}>
              创建订阅
            </Button>
            <Button onClick={() => void refreshSubscriptions()} loading={loadingList}>
              刷新列表
            </Button>
          </Space>
        </Form>

        {actionMessage ? (
          <Alert className="top-gap" type="info" showIcon title={actionMessage} />
        ) : null}
      </Card>

      <Card
        className="workspace-card"
        title="订阅总览"
        extra={<Tag color="blue">{subscriptions.length} 个活动订阅</Tag>}
      >
        {listError ? (
          <Alert type="warning" showIcon title={listError} className="bottom-gap" />
        ) : null}

        <Table
          rowKey="subscription_id"
          loading={loadingList}
          columns={[
            ...columns,
            {
              title: '操作',
              key: 'actions',
              width: 240,
              render: (_value, record) => (
                <Space wrap>
                  <Button size="small" onClick={() => void handleInspect(record)}>
                    查看详情
                  </Button>
                  <Button size="small" onClick={() => onOpenStream(record.subscription_id)}>
                    连接推送
                  </Button>
                  <Button size="small" danger onClick={() => void handleDelete(record)}>
                    取消订阅
                  </Button>
                </Space>
              ),
            },
          ]}
          dataSource={subscriptions}
          pagination={{ pageSize: 6, hideOnSinglePage: true }}
        />
      </Card>

      <Drawer
        title="订阅详情"
        size="large"
        open={detailOpen}
        onClose={() => setDetailOpen(false)}
      >
        {detailLoading ? <Typography.Text>正在加载详情...</Typography.Text> : null}
        {detail ? (
          <>
            <Descriptions
              column={1}
              size="small"
              items={[
                {
                  label: '订阅 ID',
                  children: <Typography.Text code>{detail.subscription_id}</Typography.Text>,
                },
                { label: '股票代码', children: detail.symbols.join(', ') },
                { label: '周期', children: detail.period },
                { label: '复权', children: detail.adjust_type },
                { label: '订阅类型', children: detail.subscription_type },
                { label: '状态', children: detail.active ? 'active' : 'inactive' },
                { label: '队列积压', children: detail.queue_size },
                { label: '创建时间', children: formatTimestamp(detail.created_at) },
                { label: '最近心跳', children: formatTimestamp(detail.last_heartbeat) },
              ]}
            />

            <Typography.Title level={5}>原始 JSON</Typography.Title>
            <pre className="json-panel">{formatJson(detail)}</pre>
          </>
        ) : null}
      </Drawer>
    </div>
  )
}
