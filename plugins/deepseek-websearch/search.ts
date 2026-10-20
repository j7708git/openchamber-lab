/**
 * DeepSeek Search API 的純函式封裝。
 *
 * 這個檔案刻意**不** import `@opencode/plugin` 或 `@opencode/schema`：
 * - 可以直接用 node import 測試（Node 22 原生支援 TypeScript type stripping）。
 * - 型別自己宣告，形狀與 OpenCode 的 `WebSearch.Result` 相同。
 *
 * 職責只有三件：送出請求（含逾時與重試）、把回應映射成 `Result[]`、把錯誤分類成
 * 可讀的 `Error`。金鑰解析、註冊 provider 由 `index.ts` 負責。
 */

/** DeepSeek 官方 Search API endpoint。 */
export const DEEPSEEK_SEARCH_ENDPOINT = "https://api.deepseek.com/v1/tools/search"

/** query 長度上限（Unicode 字元，DeepSeek 規格為 1–128）。 */
export const MAX_QUERY_LENGTH = 128

export const DEFAULT_COUNT = 10
export const DEFAULT_TIMEOUT_MS = 15_000
export const DEFAULT_MAX_RETRIES = 2

/** 單次退避等待上限（毫秒），含 `Retry-After` 的夾值。 */
export const MAX_BACKOFF_MS = 5_000

/** 沒有金鑰時的統一訊息：同時告訴使用者 env 與 credential store 兩種設定方式。 */
export const MISSING_API_KEY_MESSAGE =
  "尚未設定 DeepSeek 金鑰：請設定環境變數 DEEPSEEK_API_KEY，或在 OpenChamber 的 credential store 加入 deepseek 金鑰。"

/**
 * OpenCode websearch 的 `Result` 形狀。
 *
 * `time` 是必填物件（沒有日期也要給 `{}`）；`published` 是 epoch **毫秒**。
 */
export interface Result {
  url: string
  title?: string
  content?: string
  time: { published?: number }
}

export interface SearchOptions {
  /** 回傳筆數，1–50，預設 10。 */
  count?: number
  /** 是否要求 DeepSeek 回傳完整頁面內容，預設 false。 */
  needContent?: boolean
  /** 選填的 localization（原樣送給 DeepSeek），預設不送。 */
  localization?: string
  /** 整次搜尋（含重試與退避）的逾時毫秒數，預設 15000。 */
  timeoutMs?: number
  /** 可重試錯誤的最大重試次數，預設 2。 */
  maxRetries?: number
}

/** 方便注入 stub 的 fetch 形狀（只用到最簡單的呼叫形式）。 */
export type FetchLike = (input: string, init?: RequestInit) => Promise<Response>

export interface SearchInput {
  query: string
  apiKey: string
  /** 上層（OpenCode execute）傳進來的取消訊號。 */
  signal?: AbortSignal
  options?: SearchOptions
  /** 測試用；預設用全域 `fetch`。 */
  fetchImpl?: FetchLike
}

interface NormalizedOptions {
  count: number
  needContent: boolean
  localization?: string
  timeoutMs: number
  maxRetries: number
}

function readInteger(value: unknown, fallback: number): number {
  if (typeof value === "number" && Number.isFinite(value)) return Math.trunc(value)
  if (typeof value === "string" && value.trim().length > 0) {
    const n = Number(value)
    if (Number.isFinite(n)) return Math.trunc(n)
  }
  return fallback
}

function normalizeOptions(options: SearchOptions | undefined): NormalizedOptions {
  const raw = (options ?? {}) as Record<string, unknown>
  const count = Math.min(Math.max(readInteger(raw.count, DEFAULT_COUNT), 1), 50)
  const timeoutMs = readInteger(raw.timeoutMs, DEFAULT_TIMEOUT_MS)
  const maxRetries = readInteger(raw.maxRetries, DEFAULT_MAX_RETRIES)
  const localization = typeof raw.localization === "string" ? raw.localization.trim() : ""
  return {
    count,
    needContent: raw.needContent === true,
    ...(localization ? { localization } : {}),
    timeoutMs: timeoutMs > 0 ? timeoutMs : DEFAULT_TIMEOUT_MS,
    maxRetries: maxRetries >= 0 ? maxRetries : DEFAULT_MAX_RETRIES,
  }
}

/** 把 `content` 的挑選規則集中在一處：summary → snippet →（need_content 時的）content。 */
function pickContent(hit: Record<string, unknown>, needContent: boolean): string | undefined {
  for (const field of ["summary", "snippet"] as const) {
    const value = hit[field]
    if (typeof value === "string" && value.trim().length > 0) return value.trim()
  }
  if (needContent && typeof hit.content === "string" && hit.content.trim().length > 0) {
    return hit.content.trim()
  }
  return undefined
}

/**
 * DeepSeek `results[]` → OpenCode `Result[]`。
 *
 * - `url` 必填；缺／非字串／空字串的結果整筆跳過。
 * - `title` 只放非空字串。
 * - 保留 API 原順序（已是 rerank 後排序），不輸出其他欄位。
 */
function toResults(payload: Record<string, unknown> | undefined, needContent: boolean): Result[] {
  const hits = payload?.results
  if (!Array.isArray(hits)) return []

  const results: Result[] = []
  for (const raw of hits) {
    if (typeof raw !== "object" || raw === null) continue
    const hit = raw as Record<string, unknown>

    const url = typeof hit.url === "string" ? hit.url.trim() : ""
    if (url.length === 0) continue

    const result: Result = { url, time: {} }
    if (typeof hit.title === "string" && hit.title.trim().length > 0) {
      result.title = hit.title.trim()
    }
    const content = pickContent(hit, needContent)
    if (content !== undefined) result.content = content

    // date 可能是 null 或非 RFC3339；只有能解析成有限數字才放進 published（毫秒）。
    const published = typeof hit.date === "string" ? Date.parse(hit.date) : Number.NaN
    if (Number.isFinite(published)) result.time.published = published

    results.push(result)
  }
  return results
}

function isRetryableStatus(status: number): boolean {
  return status === 408 || status === 429 || (status >= 500 && status <= 599)
}

/** `Retry-After` 支援秒數與 HTTP-date，統一換成毫秒並夾在 0–5000。 */
function parseRetryAfter(value: string | null | undefined): number | undefined {
  if (typeof value !== "string") return undefined
  const text = value.trim()
  if (text.length === 0) return undefined
  if (/^\d+$/.test(text)) {
    const seconds = Number(text)
    if (!Number.isFinite(seconds)) return undefined
    return Math.min(seconds * 1000, MAX_BACKOFF_MS)
  }
  const at = Date.parse(text)
  if (!Number.isFinite(at)) return undefined
  return Math.min(Math.max(at - Date.now(), 0), MAX_BACKOFF_MS)
}

/** 指數退避：第 0 次 500ms、第 1 次 1s…，上限 5s。 */
function backoffDelay(attempt: number): number {
  return Math.min(500 * 2 ** attempt, MAX_BACKOFF_MS)
}

/** 絕不讓金鑰出現在任何錯誤訊息或 log 字串裡（即使上游把它回顯）。 */
function redact(text: string, apiKey: string): string {
  return apiKey.length > 0 ? text.split(apiKey).join("[redacted]") : text
}

function abortError(message: string): Error {
  const error = new Error(message)
  error.name = "AbortError"
  return error
}

function isAbortError(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    (error as { name?: unknown }).name === "AbortError"
  )
}

/** 可被 `signal` 打斷的等待；不用不可中斷的 setTimeout 迴圈。 */
function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(abortError("aborted"))
      return
    }
    const onAbort = () => {
      clearTimeout(timer)
      reject(abortError("aborted"))
    }
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", onAbort)
      resolve()
    }, ms)
    signal.addEventListener("abort", onAbort, { once: true })
  })
}

/** 讀取 API 的 `error.message`；只作為錯誤訊息的補充，讀不到就省略。 */
async function readApiErrorMessage(
  response: Response,
  apiKey: string,
): Promise<string | undefined> {
  try {
    const text = await response.text()
    if (text.trim().length === 0) return undefined
    try {
      const body = JSON.parse(text) as { error?: { message?: unknown } }
      const message = body?.error?.message
      if (typeof message === "string" && message.trim().length > 0) {
        return redact(message.replace(/\s+/g, " ").trim(), apiKey).slice(0, 300)
      }
    } catch {
      // 回應不是 JSON（例如 proxy 的 HTML 錯誤頁）：不把整頁塞進錯誤訊息。
    }
    return undefined
  } catch {
    // 讀 body 失敗不影響主要錯誤（status 已足以說明），因此只省略補充訊息。
    return undefined
  }
}

/**
 * 呼叫 DeepSeek Search API 並回傳 OpenCode 可用的結果。
 *
 * 錯誤一律以 `Error` 拋出（不回空陣列假裝成功）；訊息不含金鑰或完整 header。
 * 空 query／過長 query 會在任何網路請求之前就拋錯。
 */
export async function searchDeepSeek(input: SearchInput): Promise<Result[]> {
  const query = typeof input?.query === "string" ? input.query.trim() : ""
  if (query.length === 0) {
    throw new Error(`DeepSeek 搜尋的 query 不可為空（長度限制 1–${MAX_QUERY_LENGTH} 字元）。`)
  }
  const queryLength = Array.from(query).length
  if (queryLength > MAX_QUERY_LENGTH) {
    throw new Error(
      `DeepSeek 搜尋的 query 過長（${queryLength} 字元，上限 ${MAX_QUERY_LENGTH} 字元）。`,
    )
  }

  const apiKey = typeof input?.apiKey === "string" ? input.apiKey.trim() : ""
  if (apiKey.length === 0) throw new Error(MISSING_API_KEY_MESSAGE)

  const settings = normalizeOptions(input.options)
  const fetchImpl: FetchLike = input.fetchImpl ?? ((url, init) => fetch(url, init))
  const body = JSON.stringify({
    query,
    count: settings.count,
    need_content: settings.needContent,
    ...(settings.localization ? { localization: settings.localization } : {}),
  })

  // 逾時涵蓋整次搜尋（含所有重試與退避）；外部 signal 一併串進同一個 controller。
  const controller = new AbortController()
  let timedOut = false
  const timer = setTimeout(() => {
    timedOut = true
    controller.abort()
  }, settings.timeoutMs)

  const externalSignal = input.signal
  const onExternalAbort = () => controller.abort()
  if (externalSignal) {
    if (externalSignal.aborted) controller.abort()
    else externalSignal.addEventListener("abort", onExternalAbort, { once: true })
  }

  /** 把 abort 區分成「上層取消」與「自己逾時」，訊息才不會誤導。 */
  const abortFailure = (): Error => {
    if (externalSignal?.aborted) return abortError("DeepSeek 搜尋已取消。")
    if (timedOut) return new Error(`DeepSeek 搜尋逾時（${settings.timeoutMs} 毫秒）。`)
    return abortError("DeepSeek 搜尋已取消。")
  }

  try {
    for (let attempt = 0; ; attempt += 1) {
      let response: Response
      try {
        response = await fetchImpl(DEEPSEEK_SEARCH_ENDPOINT, {
          method: "POST",
          headers: {
            authorization: `Bearer ${apiKey}`,
            "content-type": "application/json",
            accept: "application/json",
          },
          body,
          signal: controller.signal,
        })
      } catch (error) {
        if (isAbortError(error) || controller.signal.aborted) throw abortFailure()
        const reason = redact(describeError(error), apiKey)
        throw new Error(`DeepSeek 搜尋網路錯誤：${reason}`)
      }

      if (response.ok) {
        const text = await response.text()
        let payload: Record<string, unknown> | undefined
        if (text.trim().length > 0) {
          try {
            const parsed: unknown = JSON.parse(text)
            payload =
              typeof parsed === "object" && parsed !== null
                ? (parsed as Record<string, unknown>)
                : undefined
          } catch {
            throw new Error("DeepSeek 搜尋回應格式錯誤（不是合法的 JSON）。")
          }
        }
        return toResults(payload, settings.needContent)
      }

      const status = response.status

      if (status === 401 || status === 403) {
        throw new Error(`DeepSeek 金鑰無效或沒有權限（HTTP ${status}）。`)
      }
      if (status === 402) {
        throw new Error("DeepSeek 餘額不足（HTTP 402），請先儲值。")
      }

      if (!isRetryableStatus(status)) {
        const detail = await readApiErrorMessage(response, apiKey)
        throw new Error(
          `DeepSeek 搜尋失敗（HTTP ${status}）${detail ? `：${detail}` : "。"}`,
        )
      }

      if (attempt >= settings.maxRetries) {
        const detail = await readApiErrorMessage(response, apiKey)
        const tried = attempt === 0 ? "未重試" : `已重試 ${attempt} 次`
        const suffix = detail ? `：${detail}` : "。"
        if (status === 429) {
          throw new Error(
            `DeepSeek 帳號層級限速（HTTP 429），${tried}${suffix}`,
          )
        }
        throw new Error(`DeepSeek 搜尋失敗（HTTP ${status}），${tried}${suffix}`)
      }

      const retryAfter =
        status === 429 ? parseRetryAfter(response.headers.get("retry-after")) : undefined
      const delay = retryAfter ?? backoffDelay(attempt)

      // 這次回應要丟棄；把 body 讀掉讓連線可以重用（錯誤細節只在最後一次讀取）。
      await response.text().catch(() => undefined)

      try {
        await sleep(delay, controller.signal)
      } catch {
        throw abortFailure()
      }
    }
  } finally {
    clearTimeout(timer)
    if (externalSignal) externalSignal.removeEventListener("abort", onExternalAbort)
  }
}

/** 把任意錯誤收斂成單行短訊息（不含 stack）。 */
export function describeError(error: unknown): string {
  const text =
    error instanceof Error ? error.message : typeof error === "string" ? error : String(error)
  const oneLine = text.replace(/\s+/g, " ").trim()
  if (oneLine.length === 0) return "未知錯誤"
  return oneLine.length > 200 ? `${oneLine.slice(0, 200)}…` : oneLine
}
