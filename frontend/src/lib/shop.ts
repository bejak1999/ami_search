/**
 * Links back to the shop.
 *
 * A product code is enough to build the URL, which matters for individual
 * graded copies: those never come back from the API with a link of their own,
 * only with their code, so without this a copy is something you can read about
 * but not go and buy.
 *
 * A product and one of its copies are addressed by different parameters and
 * neither substitutes for the other. FIGURE-184067-R is the product, a gcode;
 * FIGURE-184067-R124 is the 124th used copy of it, an scode. Asked for a copy
 * under gcode the shop answers that it has no such item - which is how every
 * link out of the buying-choices list and the shelf-life table led nowhere.
 */

/** One second-hand copy, as opposed to the product it is a copy of. */
export function isCopyCode(code: string): boolean {
  return /-R\d+$/.test(code ?? '')
}

export function shopUrl(code: string, provider = 'amiami'): string {
  if (provider !== 'amiami') return ''
  const key = isCopyCode(code) ? 'scode' : 'gcode'
  return `https://www.amiami.com/eng/detail/?${key}=${encodeURIComponent(code)}`
}
