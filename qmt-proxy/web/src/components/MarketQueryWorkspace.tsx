import {
  Alert,
  Button,
  Card,
  Collapse,
  Empty,
  Form,
  Input,
  Select,
  Space,
  Switch,
  Table,
  Tabs,
  Typography,
} from 'antd'
import { useState } from 'react'
import type { ColumnsType } from 'antd/es/table'
import { ApiError, queryMarketData, splitListInput } from '../lib/api'
import { formatCellValue, formatJson } from '../lib/format'
import {
  ADJUST_TYPE_OPTIONS,
  PERIOD_OPTIONS,
  type ConnectionConfig,
  type MarketDataRequestPayload,
  type MarketDataResponseItem,
  type MarketQueryFormValues,
} from '../types'

interface MarketQueryWorkspaceProps {
  config: ConnectionConfig
  initialValues: MarketQueryFormValues
  onPersistPreferences: (values: MarketQueryFormValues) => void
}

function buildRows(item: MarketDataResponseItem) {
  return item.data.map((entry, index) => ({
    key: `${item.stock_code}-${index}`,
    stock_code: item.stock_code,
    row_index: index + 1,
    ...entry,
  }))
}

function buildColumns(
  item: MarketDataResponseItem,
): ColumnsType<Record<string, unknown>> {
  const keys = new Set<string>(['stock_code', 'row_index'])
  buildRows(item).forEach((row) => {
    Object.keys(row).forEach((key) => keys.add(key))
  })

  return [...keys].map((key) => ({
    title: key,
    dataIndex: key,
    key,
    render: (value: unknown) => formatCellValue(value),
  }))
}

export function MarketQueryWorkspace({
  config,
  initialValues,
  onPersistPreferences,
}: MarketQueryWorkspaceProps) {
  const [form] = Form.useForm<MarketQueryFormValues>()
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<MarketDataResponseItem[] | null>(null)

  async function handleSubmit(values: MarketQueryFormValues) {
    setLoading(true)
    setError(null)

    const payload: MarketDataRequestPayload = {
      stock_codes: splitListInput(values.stockCodesText),
      start_date: values.startDate,
      end_date: values.endDate,
      period: values.period,
      fields: splitListInput(values.fieldsText),
      adjust_type: values.adjustType,
      fill_data: values.fillData,
      disable_download: values.disableDownload,
    }

    try {
      const response = await queryMarketData(config, payload)
      setResult(response)
      onPersistPreferences(values)
    } catch (caught) {
      const message =
        caught instanceof ApiError ? caught.message : '市场数据查询失败'
      setError(message)
      setResult(null)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="workspace-stack">
      <Card className="workspace-card" title="市场数据查询">
        <Typography.Paragraph className="card-intro">
          首版查询面板聚焦 `POST /api/v1/data/market`，用于验证请求参数、查看返回结构，并快速定位认证或服务端错误。
        </Typography.Paragraph>

        <Form
          form={form}
          layout="vertical"
          initialValues={initialValues}
          onFinish={handleSubmit}
        >
          <div className="responsive-grid">
            <Form.Item
              label="股票代码"
              name="stockCodesText"
              rules={[{ required: true, message: '请输入股票代码' }]}
            >
              <Input.TextArea
                autoSize={{ minRows: 2, maxRows: 4 }}
                placeholder="000001.SZ,600000.SH"
              />
            </Form.Item>

            <Form.Item label="字段" name="fieldsText">
              <Input.TextArea
                autoSize={{ minRows: 2, maxRows: 4 }}
                placeholder="time,open,high,low,close,volume"
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

            <Form.Item label="结束日期" name="endDate">
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

            <Form.Item label="填充缺失数据" name="fillData" valuePropName="checked">
              <Switch />
            </Form.Item>

            <Form.Item label="禁用下载" name="disableDownload" valuePropName="checked">
              <Switch />
            </Form.Item>
          </div>

          <Space wrap>
            <Button type="primary" htmlType="submit" loading={loading} disabled={!config.apiKey}>
              执行查询
            </Button>
            <Button onClick={() => form.resetFields()}>重置表单</Button>
          </Space>
        </Form>

        {error ? (
          <Alert className="top-gap" type="error" showIcon title={error} />
        ) : null}
      </Card>

      <Card className="workspace-card" title="查询结果">
        {!result && !loading && !error ? (
          <Empty description="尚未执行查询" />
        ) : null}

        {result ? (
          <Tabs
            items={[
              {
                key: 'structured',
                label: '结构化视图',
                children: (
                  <Collapse
                    items={result.map((item) => ({
                      key: item.stock_code,
                      label: `${item.stock_code} · ${item.data.length} 行`,
                      children: (
                        <Table
                          rowKey="key"
                          columns={buildColumns(item)}
                          dataSource={buildRows(item)}
                          pagination={{ pageSize: 5 }}
                          scroll={{ x: true }}
                        />
                      ),
                    }))}
                  />
                ),
              },
              {
                key: 'raw',
                label: '原始 JSON',
                children: <pre className="json-panel">{formatJson(result)}</pre>,
              },
            ]}
          />
        ) : null}
      </Card>
    </div>
  )
}
