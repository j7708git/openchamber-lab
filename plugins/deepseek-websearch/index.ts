import { Plugin } from "@opencode/plugin"

import {
  describeError,
  MISSING_API_KEY_MESSAGE,
  searchDeepSeek,
  type Result,
  type SearchOptions,
} from "./search"

/**
 * deepseek-websearch plugin：把 DeepSeek 官方 Search API 註冊成 OpenCode 的
 * websearch provider。
 *
 * 註冊後的 provider 會出現在 `GET /api/websearch/provider` 與 OpenChamber
 * 「設定 → 網頁搜尋」，`id` 是 `deepseek`、顯示名稱是 `DeepSeek`。
 *
 * 這個 plugin **不會**把 DeepSeek 設成預設 provider：預設交給 config 的
 * `websearch.provider` 或設定頁的選擇，避免覆蓋使用者的決定。
 */

const PROVIDER_ID = "deepseek"
const PROVIDER_NAME = "DeepSeek"
const INTEGRATION_ID = "deepseek"
const ENV_NAME = "DEEPSEEK_API_KEY"

type KeySource = "env" | "credential" | "none"

interface ResolvedKey {
  key?: string
  source: KeySource
  /** credential 讀取失敗時的原因（只放進 log，不含值）。 */
  problem?: string
}

/**
 * 跨 runtime 讀環境變數：不直接依賴 `process` 的型別，這樣 `tsc` 不需要
 * `@types/node`。值一律 trim，空字串視為未設定。
 */
function readEnv(name: string): string | undefined {
  const env = (globalThis as { process?: { env?: Record<string, string | undefined> } }).process?.env
  const value = env?.[name]
  if (typeof value !== "string") return undefined
  const trimmed = value.trim()
  return trimmed.length > 0 ? trimmed : undefined
}

function sourceLabel(source: KeySource): string {
  switch (source) {
    case "env":
      return "env"
    case "credential":
      return "credential"
    case "none":
      return "未設定"
  }
}

/** 錯誤訊息可能被上游回顯金鑰；log 前再抹一次。 */
function redact(text: string, apiKey: string): string {
  return apiKey.length > 0 ? text.split(apiKey).join("[redacted]") : text
}

export const DeepSeekWebSearchPlugin = Plugin.define({
  id: "deepseek-websearch",
  async setup(ctx) {
    /**
     * 金鑰解析順序：`DEEPSEEK_API_KEY` → credential store 的 `deepseek`（active）。
     *
     * 每次 `execute` 都重新解析，credential store 的變更不需重啟 server 就生效；
     * 環境變數則是 server 啟動時的快照。
     */
    async function resolveKey(): Promise<ResolvedKey> {
      const fromEnv = readEnv(ENV_NAME)
      if (fromEnv) return { key: fromEnv, source: "env" }

      try {
        const connection = await ctx.integration.connection.active(INTEGRATION_ID)
        if (!connection) return { source: "none" }
        const credential = await ctx.integration.connection.resolve(connection)
        const key = credential?.type === "key" ? credential.key.trim() : ""
        if (key.length > 0) return { key, source: "credential" }
      } catch (error) {
        // active()／resolve() 在只有 env、沒有 credential 等情況下可能失敗；
        // 當成「沒有 credential 金鑰」，把原因留給載入時那一行 log。
        return { source: "none", problem: describeError(error) }
      }
      return { source: "none" }
    }

    await ctx.websearch.transform((editor) => {
      // transform callback 必須同步：金鑰與網路請求都留到 execute 才做。
      editor.add({
        id: PROVIDER_ID,
        name: PROVIDER_NAME,
        execute: async ({ query }, { signal }): Promise<Result[]> => {
          const resolved = await resolveKey()
          if (!resolved.key) {
            const reason = resolved.problem ? `（讀取 credential 失敗：${resolved.problem}）` : ""
            console.error(`[deepseek-websearch] 沒有可用的 DeepSeek 金鑰${reason}`)
            throw new Error(MISSING_API_KEY_MESSAGE)
          }

          try {
            const options = ctx.options as SearchOptions
            return await searchDeepSeek({
              query,
              apiKey: resolved.key,
              signal,
              options,
              fetchImpl: (url, init) => fetch(url, init),
            })
          } catch (error) {
            // server 會把 provider 的例外收斂成 HTTP 503，body 只有
            // "Web search request failed: deepseek"，原因不會回給 client；
            // 所以在 server log 留一行（不含金鑰、不含 query 全文）。
            console.error(
              `[deepseek-websearch] 搜尋失敗：${redact(describeError(error), resolved.key)}`,
            )
            throw error
          }
        },
      })
      // 不在這裡呼叫 editor.default.set(...)：預設 provider 交給 config
      // 的 websearch.provider 或設定頁的選擇。
    })

    const initial = await resolveKey()
    const source = initial.problem
      ? `${sourceLabel(initial.source)}（讀取 credential 失敗：${initial.problem}）`
      : sourceLabel(initial.source)
    console.log(
      `[deepseek-websearch] provider 已註冊（id: ${PROVIDER_ID}, name: ${PROVIDER_NAME}）；金鑰來源：${source}`,
    )
  },
})

export default DeepSeekWebSearchPlugin
