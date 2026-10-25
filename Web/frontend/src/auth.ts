export type Role = 'admin' | 'field'

export type Session = {
  username: string
  role: Role
}

export class AuthError extends Error {
  constructor(message: string, readonly status: number) {
    super(message)
  }
}

export async function login(username: string, password: string): Promise<Session> {
  const response = await fetch('/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
  if (response.status === 401) throw new AuthError('用户名或密码不正确。', 401)
  if (!response.ok) throw new AuthError('暂时无法登录，请稍后重试。', response.status)
  return await response.json() as Session
}

export async function logout(): Promise<void> {
  await fetch('/api/auth/logout', { method: 'POST' }).catch(() => undefined)
}

export async function fetchSession(): Promise<Session | null> {
  try {
    const response = await fetch('/api/auth/me')
    if (response.status === 401) return null
    if (!response.ok) return null
    return await response.json() as Session
  } catch {
    return null
  }
}

export function homeForRole(role: Role): string {
  // '/' 现在是公开落地页兼登录入口，管理员的“家”在 /workspace。
  return role === 'field' ? '/field' : '/workspace'
}
