import { DatabaseOutlined, RadarChartOutlined, ThunderboltOutlined } from '@ant-design/icons'
import { Badge, Card, Layout, Space, Tabs, Tag, Typography } from 'antd'
import { useState } from 'react'
import { ConnectionSettings } from './components/ConnectionSettings'
import { MarketQueryWorkspace } from './components/MarketQueryWorkspace'
import { StreamWorkspace } from './components/StreamWorkspace'
import { SubscriptionsWorkspace } from './components/SubscriptionsWorkspace'
import { loadUiPreferences, saveUiPreferences } from './lib/storage'
import type {
  ConnectionConfig,
  MarketQueryFormValues,
  SubscriptionFormValues,
  SubscriptionInfo,
  UiPreferences,
} from './types'
import './App.css'

const { Content } = Layout

function updatePreferences(
  current: UiPreferences,
  patch: Partial<UiPreferences>,
): UiPreferences {
  return {
    ...current,
    ...patch,
    connection: patch.connection ?? current.connection,
    lastSubscription: patch.lastSubscription ?? current.lastSubscription,
    lastMarketQuery: patch.lastMarketQuery ?? current.lastMarketQuery,
  }
}

function App() {
  const [preferences, setPreferences] = useState<UiPreferences>(() =>
    loadUiPreferences(),
  )
  const [activeTab, setActiveTab] = useState('subscriptions')
  const [subscriptions, setSubscriptions] = useState<SubscriptionInfo[]>([])
  const [selectedStreamSubscriptionId, setSelectedStreamSubscriptionId] =
    useState('')

  function persistPatch(patch: Partial<UiPreferences>) {
    setPreferences((current) => {
      const next = updatePreferences(current, patch)
      saveUiPreferences(next)
      return next
    })
  }

  function handleConnectionSave(connection: ConnectionConfig) {
    persistPatch({ connection })
  }

  function handleSubscriptionPreferenceSave(values: SubscriptionFormValues) {
    persistPatch({ lastSubscription: values })
  }

  function handleQueryPreferenceSave(values: MarketQueryFormValues) {
    persistPatch({ lastMarketQuery: values })
  }

  function openStreamFor(subscriptionId: string) {
    setSelectedStreamSubscriptionId(subscriptionId)
    setActiveTab('stream')
  }

  return (
    <div className="app-shell">
      <div className="grain" />
      <Layout className="workspace-layout">
        <Content className="workspace-content">
          <header className="hero-panel">
            <div className="hero-copy">
              <Space wrap>
                <Tag color="cyan">qmt-proxy /ui</Tag>
                <Tag color="gold">REST + WebSocket Workbench</Tag>
              </Space>
              <Typography.Title className="hero-title">
                Subscription Deck
              </Typography.Title>
              <Typography.Paragraph className="hero-paragraph">
                在一个工作台里查看当前订阅、追踪实时推送，并直接发起市场数据查询。所有调用都复用现有 qmt-proxy 后端契约，不引入新的接口层。
              </Typography.Paragraph>
            </div>

            <Card className="hero-card" bordered={false}>
              <Space direction="vertical" size={12}>
                <div>
                  <Typography.Text type="secondary">当前后端</Typography.Text>
                  <Typography.Title level={4}>
                    {preferences.connection.baseUrl}
                  </Typography.Title>
                </div>
                <div className="hero-stats">
                  <div>
                    <Badge status={preferences.connection.apiKey ? 'success' : 'warning'} />
                    <Typography.Text>Bearer 配置</Typography.Text>
                  </div>
                  <div>
                    <Badge status={subscriptions.length ? 'processing' : 'default'} />
                    <Typography.Text>{subscriptions.length} 个可见订阅</Typography.Text>
                  </div>
                  <div>
                    <Badge status={selectedStreamSubscriptionId ? 'processing' : 'default'} />
                    <Typography.Text>
                      {selectedStreamSubscriptionId || '尚未选择推送目标'}
                    </Typography.Text>
                  </div>
                </div>
              </Space>
            </Card>
          </header>

          <ConnectionSettings
            config={preferences.connection}
            onSave={handleConnectionSave}
          />

          <Tabs
            className="workspace-tabs"
            activeKey={activeTab}
            onChange={setActiveTab}
            items={[
              {
                key: 'subscriptions',
                label: (
                  <span>
                    <RadarChartOutlined /> 订阅管理
                  </span>
                ),
                children: (
                  <SubscriptionsWorkspace
                    config={preferences.connection}
                    initialValues={preferences.lastSubscription}
                    onPersistPreferences={handleSubscriptionPreferenceSave}
                    onSubscriptionsChange={setSubscriptions}
                    onOpenStream={openStreamFor}
                  />
                ),
              },
              {
                key: 'stream',
                label: (
                  <span>
                    <ThunderboltOutlined /> 实时推送
                  </span>
                ),
                children: (
                  <StreamWorkspace
                    config={preferences.connection}
                    subscriptions={subscriptions}
                    selectedSubscriptionId={selectedStreamSubscriptionId}
                    onSelectedSubscriptionChange={setSelectedStreamSubscriptionId}
                  />
                ),
              },
              {
                key: 'query',
                label: (
                  <span>
                    <DatabaseOutlined /> 市场查询
                  </span>
                ),
                children: (
                  <MarketQueryWorkspace
                    config={preferences.connection}
                    initialValues={preferences.lastMarketQuery}
                    onPersistPreferences={handleQueryPreferenceSave}
                  />
                ),
              },
            ]}
          />
        </Content>
      </Layout>
    </div>
  )
}

export default App
