/** 手机号登录/注册/绑定 API（验证码防爆破由后端承担） */
import { get, post } from './http'
import { type TokenPair } from './http'

/** Turnstile 人机校验配置（site_key 空 = 未启用，前端不渲染挂件） */
export function getTurnstileConfig(): Promise<{ site_key: string; enabled: boolean }> {
  return get('/api/phone/turnstile-config')
}

export function sendPhoneCode(phone: string, turnstileToken = ''): Promise<{ message: string }> {
  return post('/api/phone/send-code', { phone, turnstile_token: turnstileToken })
}

export interface PhoneLoginResult extends TokenPair {
  username: string
  is_new: boolean
  default_password_hint: boolean
}

/** 验证码登录（自动注册） */
export function phoneLogin(phone: string, code: string): Promise<PhoneLoginResult> {
  return post('/api/phone/login', { phone, code })
}

export interface PhoneRegisterResult extends TokenPair {
  username: string
  is_new: boolean
  default_password_hint: boolean
}

/** 手机号注册（带密码与用户协议勾选） */
export function phoneRegister(phone: string, code: string, password: string, agree: boolean): Promise<PhoneRegisterResult> {
  return post('/api/phone/register', { phone, code, password, agree })
}

export interface BindPhoneResult extends TokenPair {
  username: string
  renamed: boolean
  message: string
}

/** 绑定手机号（后端可能改绑 username，必须换用返回的新 token 对） */
export function bindPhone(phone: string, code: string): Promise<BindPhoneResult> {
  return post('/api/user/bind-phone', { phone, code })
}
