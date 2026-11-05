export type TokenStatus = 'active' | 'expired' | 'revoked';
export function tokenStatus(item: { revoked_at: string | null; expires_at: string | null }, now = Date.now()): TokenStatus {
  if (item.revoked_at) return 'revoked';
  if (item.expires_at && Date.parse(item.expires_at) <= now) return 'expired';
  return 'active';
}
