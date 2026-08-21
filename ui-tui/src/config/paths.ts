import { homedir } from 'node:os'
import { join } from 'node:path'

export const getmycoderHome = (env: NodeJS.ProcessEnv = process.env) => env.mycoder_HOME?.trim() || join(homedir(), '.mycoder')

export const getmycoderHomeLabel = (env: NodeJS.ProcessEnv = process.env) => env.mycoder_HOME?.trim() || '~/.mycoder'
