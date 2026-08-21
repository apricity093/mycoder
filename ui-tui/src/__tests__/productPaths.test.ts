import { homedir } from 'node:os'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

import { getmycoderHome, getmycoderHomeLabel } from '../config/paths.js'

describe('mycoder product paths', () => {
  it('uses the mycoder home override for persistent TUI state', () => {
    expect(getmycoderHome({ mycoder_HOME: '/tmp/mycoder-home' })).toBe('/tmp/mycoder-home')
    expect(getmycoderHomeLabel({ mycoder_HOME: '/tmp/mycoder-home' })).toBe('/tmp/mycoder-home')
  })

  it('uses ~/.mycoder when the override is empty', () => {
    expect(getmycoderHome({ mycoder_HOME: '  ' })).toBe(join(homedir(), '.mycoder'))
    expect(getmycoderHomeLabel({ mycoder_HOME: '  ' })).toBe('~/.mycoder')
  })
})
