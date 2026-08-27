/**
 * Links back to the shop.
 *
 * A product code is enough to build the URL, which matters for individual
 * graded copies: those never come back from the API with a link of their own,
 * only with their code, so without this a copy is something you can read about
 * but not go and buy.
 */
export function shopUrl(code: string, provider = 'amiami'): string {
  if (provider !== 'amiami') return ''
  return `https://www.amiami.com/eng/detail/?gcode=${encodeURIComponent(code)}`
}
