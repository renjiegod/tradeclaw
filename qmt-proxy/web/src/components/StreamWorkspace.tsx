import {
  Alert,
  AutoComplete,
  Button,
  Card,
  Descriptions,
  Space,
  Tag,
  Typography,
} from 'antd'
import { useEffect, useRef, useState } from 'react'
import { formatJson, formatTimestamp } from '../lib/format'
import { buildWebSocketUrl, parseSocketMessage } from '../lib/ws'
import type {
  ConnectionConfig,
  QuoteSocketMessage,
  SubscriptionInfo,
} from '../types'

interface StreamWorkspaceProps {
  config: ConnectionConfig
  subscriptions: SubscriptionInfo[]
  selectedSubscriptionId: string
  onSelectedSubscriptionChange: (subscriptionId: string) => void
}

const MAX_STREAM_MESSAGES = 200

export function StreamWorkspace({
  config,
  subscriptions,
  selectedSubscriptionId,
  onSelectedSubscriptionChange,
}: StreamWorkspaceProps) {
  const [targetId, setTargetId] = useState(selectedSubscriptionId)
  const [status, setStatus] = useState<'idle' | 'connecting' | 'connected' | 'closed' | 'error'>('idle')
  const [statusNote, setStatusNote] = useState('尚未建立 WebSocket 连接')
  const [streamError, setStreamError] = useState<string | null>(null)
  const [lastPongAt, setLastPongAt] = useState<string | null>(null)
  const [historyById, setHistoryById] = useState<Record<string, QuoteSocketMessage[]>>({})

  const socketRef = useRef<WebSocket | null>(null)
  const heartbeatRef = useRef<number | null>(null)

  useEffect(() => {
    setTargetId(selectedSubscriptionId)
  }, [selectedSubscriptionId])

  useEffect(() => {
    return () => {
      disconnect()
    }
  }, [])

  function clearHeartbeat() {
    if (heartbeatRef.current !== null) {
      window.clearInterval(heartbeatRef.current)
      heartbeatRef.current = null
    }
  }

  function appendMessage(subscriptionId: string, message: QuoteSocketMessage) {
    setHistoryById((previous) => {
      const next = [...(previous[subscriptionId] ?? []), message].slice(
        -MAX_STREAM_MESSAGES,
      )
      return {
        ...previous,
        [subscriptionId]: next,
      }
    })
  }

  function disconnect() {
    clearHeartbeat()
    if (socketRef.current) {
      socketRef.current.close()
      socketRef.current = null
    }
  }

  function connect() {
    if (!targetId) {
      setStreamError('请先输入或选择一个 subscription_id')
      return
    }

    disconnect()
    onSelectedSubscriptionChange(targetId)
    setStatus('connecting')
    setStatusNote('正在建立 WebSocket 连接...')
    setStreamError(null)
    setLastPongAt(null)

    const socket = new WebSocket(
      buildWebSocketUrl(config.baseUrl, `/ws/quote/${targetId}`),
    )

    socketRef.current = socket

    socket.onopen = () => {
      setStatusNote('底层连接已打开，等待服务端 connected 事件')
      clearHeartbeat()
      heartbeatRef.current = window.setInterval(() => {
        if (socket.readyState === WebSocket.OPEN) {
          socket.send(JSON.stringify({ type: 'ping' }))
        }
      }, 15000)
    }

    socket.onmessage = (event) => {
      const payload = parseSocketMessage(String(event.data))
      appendMessage(targetId, payload)

      if (payload.type === 'connected') {
        setStatus('connected')
        setStatusNote('推送通道已连接')
      } else if (payload.type === 'pong') {
        setLastPongAt(new Date().toISOString())
      } else if (payload.type === 'error') {
        setStatus('error')
        setStreamError(payload.message ?? '订阅推送返回错误')
        setStatusNote('推送通道返回错误事件')
      }
    }

    socket.onerror = () => {
      setStatus('error')
      setStatusNote('WebSocket 连接失败')
      setStreamError('无法连接到推送通道')
    }

    socket.onclose = () => {
      clearHeartbeat()
      setStatus((previous) => (previous === 'error' ? previous : 'closed'))
      setStatusNote('WebSocket 已关闭')
    }
  }

  const streamMessages = targetId ? historyById[targetId] ?? [] : []
  const latestMessage = streamMessages.at(-1)

  return (
    <div className="workspace-stack">
      <Card className="workspace-card" title="实时推送通道">
        <Typography.Paragraph className="card-intro">
          这里直接连接后端 `/ws/quote/{'{subscription_id}'}`，保留每个订阅最近
          {MAX_STREAM_MESSAGES} 条消息，用来观察 `connected`、`pong`、`quote`
          和错误事件。
        </Typography.Paragraph>

        <div className="stream-toolbar">
          <AutoComplete
            className="stream-target"
            value={targetId}
            options={subscriptions.map((item) => ({
              value: item.subscription_id,
              label: `${item.subscription_id} · ${item.symbols.join(', ')}`,
            }))}
            onChange={(value) => setTargetId(value)}
            placeholder="输入或选择 subscription_id"
          />

          <Space wrap>
            <Button type="primary" onClick={connect} disabled={!config.apiKey}>
              开始连接
            </Button>
            <Button onClick={disconnect}>断开连接</Button>
            <Button
              onClick={() => {
                if (!targetId) {
                  return
                }

                setHistoryById((previous) => ({
                  ...previous,
                  [targetId]: [],
                }))
              }}
            >
              清空缓存
            </Button>
          </Space>
        </div>

        <Descriptions
          className="top-gap"
          column={3}
          items={[
            {
              label: '连接状态',
              children: (
                <Tag color={status === 'connected' ? 'green' : status === 'error' ? 'red' : 'blue'}>
                  {status}
                </Tag>
              ),
            },
            { label: '状态说明', children: statusNote },
            { label: '最近心跳', children: lastPongAt ? formatTimestamp(lastPongAt) : '尚未收到 pong' },
            { label: '当前订阅', children: targetId || '未选择' },
            { label: '缓存消息数', children: streamMessages.length },
            { label: '最新事件类型', children: latestMessage?.type ?? '-' },
          ]}
        />

        {streamError ? (
          <Alert className="top-gap" type="error" showIcon title={streamError} />
        ) : null}
      </Card>

      <Card className="workspace-card" title="最新消息">
        {latestMessage ? (
          <>
            <Descriptions
              column={2}
              items={[
                { label: '事件类型', children: latestMessage.type },
                { label: '时间戳', children: formatTimestamp(latestMessage.timestamp) },
                { label: '消息', children: latestMessage.message ?? '无' },
              ]}
            />
            <pre className="json-panel">{formatJson(latestMessage)}</pre>
          </>
        ) : (
          <Alert type="info" showIcon title="连接后会在这里显示最近一条推送消息" />
        )}
      </Card>

      <Card className="workspace-card" title="消息历史">
        {streamMessages.length === 0 ? (
          <Alert type="info" showIcon title="暂无推送消息" />
        ) : (
          <div className="history-list">
            {[...streamMessages].reverse().map((item, index) => (
              <article className="history-entry" key={`${item.type}-${index}`}>
                <Space wrap>
                  <Tag>{item.type}</Tag>
                  <Typography.Text type="secondary">
                    #{streamMessages.length - index}
                  </Typography.Text>
                  <Typography.Text type="secondary">
                    {formatTimestamp(item.timestamp)}
                  </Typography.Text>
                </Space>
                <pre className="mini-json">{formatJson(item)}</pre>
              </article>
            ))}
          </div>
        )}
      </Card>
    </div>
  )
}
