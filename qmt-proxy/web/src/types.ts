export type SubscriptionType = 'quote' | 'whole_quote'

export interface ConnectionConfig {
  baseUrl: string
  apiKey: string
}

export interface SubscriptionFormValues {
  symbolsText: string
  period: string
  startDate: string
  adjustType: string
  subscriptionType: SubscriptionType
}

export interface MarketQueryFormValues {
  stockCodesText: string
  startDate: string
  endDate: string
  period: string
  fieldsText: string
  adjustType: string
  fillData: boolean
  disableDownload: boolean
}

export interface UiPreferences {
  connection: ConnectionConfig
  lastSubscription: SubscriptionFormValues
  lastMarketQuery: MarketQueryFormValues
}

export interface SubscriptionInfo {
  subscription_id: string
  subids_xtquant?: number[]
  symbols: string[]
  period: string
  start_date: string
  adjust_type: string
  subscription_type: SubscriptionType
  created_at: string
  last_heartbeat: string
  active: boolean
  queue_size: number
}

export interface SubscriptionListResponse {
  subscriptions: SubscriptionInfo[]
  total: number
}

export interface SubscriptionCreateResponse {
  subscription_id: string
  status: string
  created_at: string
  symbols: string[]
  period: string
  start_date: string
  adjust_type: string
  subscription_type: SubscriptionType
  message?: string
}

export interface DeleteSubscriptionResponse {
  success: boolean
  message: string
  subscription_id: string
}

export interface MarketDataRequestPayload {
  stock_codes: string[]
  start_date: string
  end_date: string
  period: string
  fields?: string[]
  adjust_type?: string
  fill_data: boolean
  disable_download: boolean
}

export interface MarketDataResponseItem {
  stock_code: string
  data: Array<Record<string, unknown>>
  fields: string[]
  period: string
  start_date: string
  end_date: string
}

export interface QuoteSocketMessage {
  type: string
  timestamp?: string
  message?: string
  subscription_id?: string
  data?: Record<string, unknown>
}

export const PERIOD_OPTIONS = [
  'tick',
  '1m',
  '5m',
  '15m',
  '30m',
  '1h',
  '1d',
  '1w',
  '1mon',
  '1q',
  '1hy',
  '1y',
]

export const ADJUST_TYPE_OPTIONS = [
  'none',
  'front',
  'back',
  'front_ratio',
  'back_ratio',
]

export const DEFAULT_SUBSCRIPTION_FORM: SubscriptionFormValues = {
  symbolsText: '000001.SZ,600000.SH',
  period: 'tick',
  startDate: '',
  adjustType: 'none',
  subscriptionType: 'quote',
}

export const DEFAULT_MARKET_QUERY_FORM: MarketQueryFormValues = {
  stockCodesText: '000001.SZ,600000.SH',
  startDate: '',
  endDate: '',
  period: '1d',
  fieldsText: 'time,open,high,low,close,volume',
  adjustType: 'none',
  fillData: true,
  disableDownload: true,
}
