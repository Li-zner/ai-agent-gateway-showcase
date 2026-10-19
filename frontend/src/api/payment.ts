/** 模拟支付 API（钱包/充值/支付确认/流水） */
import { get, post } from './http'

export interface Wallet {
  user_id: string
  balance: number
  total_recharged: number
  total_spent: number
  total_refunded: number
  status: string
}

export interface RechargeOrder {
  order_no: string
  amount: number
  status: string
  payment_method: string
}

export interface PayResult {
  order_no: string
  status: string
  current_balance: number | null
  message: string
}

export interface Transaction {
  id: number
  order_no: string
  tx_type: string
  amount: number
  before_balance: number
  after_balance: number
  remark: string
  created_at: string
}

export interface TransactionList {
  items: Transaction[]
  total: number
  page: number
  page_size: number
  total_pages: number
}

export function getWallet(): Promise<Wallet> {
  return get('/api/payment/wallet')
}

export function createRechargeOrder(amount: number, paymentMethod: string, idempotentKey: string): Promise<RechargeOrder> {
  return post('/api/payment/recharge', {
    amount,
    payment_method: paymentMethod,
  }, { 'X-Idempotent-Key': idempotentKey })
}

export function payOrder(orderNo: string): Promise<PayResult> {
  return post(`/api/payment/${encodeURIComponent(orderNo)}/pay`)
}

export function getTransactions(page = 1, pageSize = 10, txType?: string): Promise<TransactionList> {
  const q = new URLSearchParams({ page: String(page), page_size: String(pageSize) })
  if (txType) q.set('tx_type', txType)
  return get(`/api/payment/transactions?${q}`)
}
