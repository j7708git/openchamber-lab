import { Plugin } from "@opencode/plugin"

/**
 * poll plugin：多模型諮詢。
 *
 * 讓原 session 的 agent 一次邀請多個模型，對同一個問題各發表一次性意見，
 * 意見回到同一個 session 由原 agent 綜合。意見一律走 ctx.generate.text，
 * 不開 session、不帶工具、不進歷史。
 */

/** 內建預設名單（使用者 2026-10-15 確認）。格式：providerID/id */
const DEFAULT_MODELS = [
  "opencode-go/deepseek-v4.1-flash",
  "opencode-go/qwen3.8-flash",
  "opencode-go/glm-5.3-flash",
]

const DEFAULT_MAX_WORDS = 150
const DEFAULT_TIMEOUT_MS = 60_000
const DEFAULT_EXPENSIVE_INPUT_COST_PER_MTOKEN = 1

const MODEL_FORMAT_HINT =
  "模型字串格式為 providerID/id，例如 opencode-go/deepseek-v4.1-flash"

type OpinionStatus = "ok" | "error" | "timeout" | "skipped"

interface Opinion {
  /** 原始模型字串 providerID/id */
  model: string
  status: OpinionStatus
  /** status === "ok" 時的一次性意見 */
  text?: string
  /** status === "error" 時的簡短原因 */
  error?: string
  /** status === "skipped" 時的跳過原因 */
  reason?: string
}

interface PollSettings {
  models: string[]
  maxWords: number
  timeoutMs: number
  allowExpensive: string[]
  expensiveInputCostPerMToken: number
}

interface ModelRef {
  providerID: string
  id: string
}

/** 非負整數欄位解析：讀不到或非法就用預設值 */
function readPositiveInt(value: unknown, fallback: number): number {
  const n = typeof value === "number" ? value : Number(value)
  if (!Number.isFinite(n) || n <= 0) return fallback
  return Math.floor(n)
}

function readNonNegativeNumber(value: unknown, fallback: number): number {
  const n = typeof value === "number" ? value : Number(value)
  if (!Number.isFinite(n) || n < 0) return fallback
  return n
}

function readStringArray(value: unknown): string[] | undefined {
  if (!Array.isArray(value)) return undefined
  return value.filter((item): item is string => typeof item === "string")
}

/** options 可能為 undefined；缺省一律回退內建預設 */
function readSettings(options: unknown): PollSettings {
  const raw = (options ?? {}) as Record<string, unknown>
  const models = readStringArray(raw.models)
  return {
    models: models ?? [...DEFAULT_MODELS],
    maxWords: readPositiveInt(raw.maxWords, DEFAULT_MAX_WORDS),
    timeoutMs: readPositiveInt(raw.timeoutMs, DEFAULT_TIMEOUT_MS),
    allowExpensive: readStringArray(raw.allowExpensive) ?? [],
    expensiveInputCostPerMToken: readNonNegativeNumber(
      raw.expensiveInputCostPerMToken,
      DEFAULT_EXPENSIVE_INPUT_COST_PER_MTOKEN,
    ),
  }
}

/** 以第一段 "/" 切出 providerID，其餘為 id */
function parseModelRef(model: string): ModelRef | undefined {
  const slash = model.indexOf("/")
  if (slash <= 0 || slash === model.length - 1) return undefined
  return {
    providerID: model.slice(0, slash),
    id: model.slice(slash + 1),
  }
}

/** 把錯誤收斂成簡短可讀的原因，不倒出 stack */
function briefError(error: unknown): string {
  const text =
    error instanceof Error ? error.message : typeof error === "string" ? error : String(error)
  const oneLine = text.replace(/\s+/g, " ").trim()
  if (oneLine.length === 0) return "未知錯誤"
  return oneLine.length > 200 ? `${oneLine.slice(0, 200)}…` : oneLine
}

function buildPrompt(question: string, maxWords: number): string {
  return [
    "你現在只提供一次性的諮詢意見：只回答底下的問題。",
    "你不會執行任何動作、不會讀取也不修改任何檔案、不使用工具，也不需要延續任何對話。",
    "不需要確認問題是否清楚；就你目前的理解直接給意見。",
    "",
    "問題：",
    question,
    "",
    `請用繁體中文回答，條列、簡潔，先給結論再給理由，總長不超過約 ${maxWords} 字。`,
  ].join("\n")
}

/** 把模型成本清單轉成 input 單價（USD per 1M tokens）；取不到回 undefined */
function inputCostPerMillion(modelInfo: unknown): number | undefined {
  const info = modelInfo as { cost?: unknown } | undefined
  if (!info || !Array.isArray(info.cost) || info.cost.length === 0) return undefined
  // cost 是分層陣列（可能帶 tier）；取第一層作為基準單價。
  const tier = info.cost[0] as { input?: unknown } | undefined
  if (!tier || typeof tier.input !== "number" || !Number.isFinite(tier.input)) return undefined
  return tier.input
}

function formatOpinionLine(opinion: Opinion, timeoutMs: number): string {
  switch (opinion.status) {
    case "ok":
      return `— ${opinion.model}：${opinion.text ?? "（沒有內容）"}`
    case "timeout":
      return `— ${opinion.model}：（逾時，超過 ${timeoutMs} ms 未回覆，本次沒有意見）`
    case "error":
      return `— ${opinion.model}：（失敗：${opinion.error ?? "未知錯誤"}，本次沒有意見）`
    case "skipped":
      return `— ${opinion.model}：（已跳過：${opinion.reason ?? "未提供原因"}，本次沒有意見）`
  }
}

function buildRelayMessage(
  question: string,
  opinions: Opinion[],
  settings: PollSettings,
): string {
  const names = opinions.map((opinion) => opinion.model).join("/")
  return [
    `以下邀請 ${names} 針對同一個問題各發表一次性意見（未讀專案、未動檔案）：`,
    `問題：${question}`,
    "",
    ...opinions.map((opinion) => formatOpinionLine(opinion, settings.timeoutMs)),
    "",
    "請把以上視為「其他模型的一次性轉述意見」，不是使用者的立場：逐一點評各方論點（同意／質疑／補充），綜合你自己的上下文做出建議，然後續原本的工作。有意見缺席的模型照樣根據已有的意見綜合，並提及誰沒有意見。",
  ].join("\n")
}

function buildToolReport(question: string, opinions: Opinion[], settings: PollSettings): string {
  const blocks = opinions.map((opinion) => {
    const header = `### ${opinion.model}`
    switch (opinion.status) {
      case "ok":
        return `${header}\n${opinion.text ?? "（沒有內容）"}`
      case "timeout":
        return `${header}\n（逾時，超過 ${settings.timeoutMs} ms 未回覆，本次沒有意見）`
      case "error":
        return `${header}\n（失敗：${opinion.error ?? "未知錯誤"}，本次沒有意見）`
      case "skipped":
        return `${header}\n（已跳過：${opinion.reason ?? "未提供原因"}，本次沒有意見）`
    }
  })

  return [
    "## 多方一次性意見",
    `問題：${question}`,
    "",
    blocks.join("\n\n"),
    "",
    "---",
    "這些是其他模型的一次性意見，請逐點評後再決定採納或捨棄。",
  ].join("\n")
}

export const PollPlugin = Plugin.define({
  id: "poll",
  async setup(ctx) {
    const settings = readSettings(ctx.options)
    console.log(
      `[poll] loaded in OpenCode ${ctx.app.version}（models: ${settings.models.join(", ") || "（未設定）"}）`,
    )

    /**
     * 一次邀請多顆模型對同一問題發表一次性意見，並行、互不影響。
     *
     * explicit 為 true 代表清單來自使用者當次明示指名（/poll 參數或 poll_ask 的
     * models），此時不受貴模型防呆限制。
     */
    async function askModels(
      question: string,
      models: string[],
      explicit: boolean,
    ): Promise<Opinion[]> {
      const prompt = buildPrompt(question, settings.maxWords)

      // 價格資訊只在呼叫前取一次；取不到成本資訊的模型不擋。
      let costByRef = new Map<string, number>()
      try {
        const listed = await ctx.model.list()
        const infoList = Array.isArray(listed)
          ? listed
          : ((listed as { data?: unknown })?.data as unknown[]) ?? []
        for (const info of infoList) {
          const rec = info as { id?: unknown; providerID?: unknown }
          if (typeof rec?.id !== "string" || typeof rec?.providerID !== "string") continue
          const cost = inputCostPerMillion(info)
          if (cost !== undefined) costByRef.set(`${rec.providerID}/${rec.id}`, cost)
        }
      } catch {
        // 取不到成本資訊時保守地不擋任何模型（見 README 已知限制）。
        costByRef = new Map()
      }

      const tasks = models.map(async (model): Promise<Opinion> => {
        const ref = parseModelRef(model)
        if (!ref) {
          return { model, status: "error", error: `模型格式錯誤（${MODEL_FORMAT_HINT}）` }
        }

        const cost = costByRef.get(model)
        const isExpensive =
          cost !== undefined && cost >= settings.expensiveInputCostPerMToken
        const allowed =
          explicit ||
          settings.allowExpensive.includes(model) ||
          cost === undefined ||
          !isExpensive
        if (isExpensive && !allowed) {
          return {
            model,
            status: "skipped",
            reason: `貴模型防呆：input 成本 ${cost} ≥ ${settings.expensiveInputCostPerMToken} USD/1M tokens，未在當次明示指名、也不在 allowExpensive 清單`,
          }
        }

        // 先接住 rejection，避免逾時後才失敗時變成 unhandled rejection。
        const call = ctx.generate
          .text({ model: ref, prompt })
          .then((result: { text?: string }) => ({
            state: "ok" as const,
            text: result?.text ?? "",
          }))
          .catch((error: unknown) => ({ state: "error" as const, error: briefError(error) }))

        let timer: ReturnType<typeof setTimeout> | undefined
        const timeout = new Promise<{ state: "timeout" }>((resolve) => {
          timer = setTimeout(() => resolve({ state: "timeout" }), settings.timeoutMs)
        })

        try {
          const settled = await Promise.race([call, timeout])
          if (settled.state === "ok") return { model, status: "ok", text: settled.text }
          if (settled.state === "error") return { model, status: "error", error: settled.error }
          return { model, status: "timeout" }
        } catch (error) {
          return { model, status: "error", error: briefError(error) }
        } finally {
          if (timer !== undefined) clearTimeout(timer)
        }
      })

      const settledList = await Promise.allSettled(tasks)
      return settledList.map((entry, index) =>
        entry.status === "fulfilled"
          ? entry.value
          : { model: models[index], status: "error", error: briefError(entry.reason) },
      )
    }

    /** /poll 的參數：`[model1,model2] <問題>`；模型段可省 */
    function parsePollArgs(raw: string): { models: string[]; question: string } {
      const text = (raw ?? "").trim()
      const match = text.match(/^\[([^\]]*)\]\s*([\s\S]*)$/)
      if (!match) return { models: [], question: text }
      return {
        models: match[1]
          .split(",")
          .map((item) => item.trim())
          .filter((item) => item.length > 0),
        question: match[2].trim(),
      }
    }

    await ctx.command.transform((editor) => {
      editor.add({
        name: "poll",
        description: "一次邀請多個模型對同一問題發表一次性意見，再回到這個對話綜合",
        execute: async ({ sessionID, prompt }) => {
          try {
            const { models: named, question } = parsePollArgs(prompt?.text ?? "")
            if (question.length === 0) {
              await ctx.session.prompt({
                sessionID,
                text: [
                  "（/poll）沒收到問題，所以沒有邀請任何模型。",
                  "用法：`/poll [model1,model2] 你的問題`；中括號的模型清單可省略，省略時用設定檔的預設名單。",
                ].join("\n"),
                delivery: "queue",
              })
              return
            }

            const explicit = named.length > 0
            const models = explicit ? named : settings.models
            if (models.length === 0) {
              await ctx.session.prompt({
                sessionID,
                text: [
                  "（/poll）目前沒有可用的模型名單，所以沒有邀請任何模型。",
                  "請在 opencode.jsonc 的 poll plugin options 設定 `models`，或這次直接指定：`/poll [providerID/model,providerID/model] 你的問題`。",
                ].join("\n"),
                delivery: "queue",
              })
              return
            }

            const opinions = await askModels(question, models, explicit)
            await ctx.session.prompt({
              sessionID,
              text: buildRelayMessage(question, opinions, settings),
              delivery: "queue",
            })
          } catch (error) {
            // execute 不可拋出未捕獲例外；如實回報一次。
            await ctx.session
              .prompt({
                sessionID,
                text: `（/poll）執行時發生錯誤，這次沒有取得任何意見：${briefError(error)}`,
                delivery: "queue",
              })
              .catch(() => undefined)
          }
        },
      })
    })

    await ctx.tool.transform((editor) => {
      // 期待完整工具 ID 為 poll_ask。
      editor.namespace({
        name: "poll",
        description: "多模型諮詢：一次邀請多個模型對同一問題發表一次性意見",
      })
      editor.add({
        name: "ask",
        description:
          "一次邀請多個模型對同一個問題各發表一次性意見，回傳署名的意見供你逐點評後再決定採納或捨棄。",
        input: {
          type: "object",
          properties: {
            question: {
              type: "string",
              description: "要請多個模型各自回答的問題",
            },
            models: {
              type: "array",
              items: { type: "string" },
              description: `這次要邀請的模型清單（providerID/id）。省略時用設定檔的預設名單。${MODEL_FORMAT_HINT}`,
            },
          },
          required: ["question"],
          additionalProperties: false,
        },
        options: { namespace: "poll" },
        execute: async (input) => {
          const value = (input ?? {}) as { question?: unknown; models?: unknown }
          const question = typeof value.question === "string" ? value.question.trim() : ""
          if (question.length === 0) {
            return {
              content: "poll_ask：沒有收到 question，沒有邀請任何模型。請帶上要詢問的問題。",
            }
          }

          const named = readStringArray(value.models) ?? []
          const explicit = named.length > 0
          const models = explicit ? named : settings.models
          if (models.length === 0) {
            return {
              content:
                "poll_ask：目前沒有可用的模型名單。請在 opencode.jsonc 的 poll plugin options 設定 `models`，或在這次呼叫帶上 `models`。",
            }
          }

          try {
            const opinions = await askModels(question, models, explicit)
            return { content: buildToolReport(question, opinions, settings) }
          } catch (error) {
            return { content: `poll_ask 執行時發生錯誤：${briefError(error)}` }
          }
        },
      })
    })
  },
})

export default PollPlugin
