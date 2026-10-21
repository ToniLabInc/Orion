/**
 * Tasks: your to-do list, run straight through his organizer's todo tool (the list comes back as a "todos" event). A
 * ticked task draws its check, then folds away; one dropped without being done just folds. Add one at the bottom.
 */
import { ListChecks, Plus, Trash2 } from "lucide-react"
import { AnimatePresence, motion } from "motion/react"
import { type FormEvent, useEffect, useState } from "react"
import { useOrganizer } from "@/lib/api"
import { FOLD, SPRING } from "@/lib/motion"
import { useLive } from "@/lib/store"
import type { Todo } from "@/lib/types"
import { cn } from "@/lib/utils"
import { IconButton } from "@/ui/button"
import { Input } from "@/ui/input"
import { Empty } from "@/ui/states"
import type { CardProps } from "./card"

/**
 * The tasks card.
 * @param props the card's
 * @returns the body
 */
export default function Tasks({ setNote }: CardProps) {
  const todos = useLive((s) => s.todos)
  const online = useLive((s) => s.link === "online")
  const run = useOrganizer()
  const [ticked, setTicked] = useState<number[]>([])
  const [dropping, setDropping] = useState<number[]>([])
  const [text, setText] = useState("")
  useEffect(() => setNote(todos.length ? `${todos.length} open` : null), [todos.length, setNote])

  const tick = async (todo: Todo) => {
    setTicked((t) => [...t, todo.id])
    await new Promise((resolve) => setTimeout(resolve, 420)) // the check draws, then the row folds
    if (!(await run("todo", { action: "complete", item: `#${todo.id}` }, "Tasks")))
      setTicked((t) => t.filter((id) => id !== todo.id))
  }
  const drop = async (todo: Todo) => {
    setDropping((d) => [...d, todo.id])
    if (!(await run("todo", { action: "remove", item: `#${todo.id}` }, "Tasks")))
      setDropping((d) => d.filter((id) => id !== todo.id))
  }
  const add = async (e: FormEvent) => {
    e.preventDefault()
    const item = text.trim()
    if (!item) return
    setText("")
    if (!(await run("todo", { action: "add", item }, "Tasks"))) setText(item)
  }

  return (
    <div className="grid gap-3 px-3 pt-1 pb-4" data-testid="widget-tasks">
      <ul className="grid">
        <AnimatePresence initial={false}>
          {todos.map((todo) => (
            <motion.li
              key={todo.id}
              layout
              {...FOLD}
              transition={{ ...FOLD.transition, layout: SPRING.gentle }}
              className={cn("overflow-hidden", dropping.includes(todo.id) && "opacity-40")}
            >
              <Task
                todo={todo}
                done={ticked.includes(todo.id)}
                online={online}
                onTick={() => tick(todo)}
                onDrop={() => drop(todo)}
              />
            </motion.li>
          ))}
        </AnimatePresence>
        {!todos.length && (
          <li>
            <Empty icon={<ListChecks />}>Nothing to do. Enjoy it.</Empty>
          </li>
        )}
      </ul>
      <form onSubmit={add} className="flex gap-2 px-1">
        <Input
          value={text}
          onChange={(e) => setText(e.target.value)}
          maxLength={200}
          placeholder="Add a task…"
          disabled={!online}
          aria-label="A new task"
          data-testid="task-input"
        />
        <IconButton
          label="Add the task"
          type="submit"
          tone="secondary"
          disabled={!online || !text.trim()}
          data-testid="task-add"
        >
          <Plus />
        </IconButton>
      </form>
    </div>
  )
}

/**
 * One task: a round box whose check draws when ticked, and a way to drop it.
 * @param props the `todo`, whether it is `done`, whether he is `online`, `onTick` and `onDrop`
 * @returns the task
 */
function Task({
  todo,
  done,
  online,
  onTick,
  onDrop,
}: {
  todo: Todo
  done: boolean
  online: boolean
  onTick: () => void
  onDrop: () => void
}) {
  return (
    <div className="group/task flex items-start gap-1 rounded-control px-1 transition-colors hover:bg-steel-50/4 has-focus-visible:bg-steel-50/6">
      <label className="flex min-w-0 flex-1 cursor-pointer items-start gap-3 py-2" data-testid="task">
        <input type="checkbox" checked={done} disabled={done || !online} onChange={onTick} className="peer sr-only" />
        <span
          aria-hidden="true"
          className={cn(
            "mt-px grid size-[18px] shrink-0 place-items-center rounded-full shadow-[inset_0_0_0_1.5px_var(--hairline-strong)] transition-[background-color,box-shadow] duration-(--dur-quick) group-hover/task:shadow-[inset_0_0_0_1.5px_var(--color-rigel-400)] peer-focus-visible:outline-2 peer-focus-visible:outline-rigel-400",
            done && "bg-ok-400 shadow-none",
          )}
        >
          {done && (
            <svg viewBox="0 0 16 16" aria-hidden="true" className="size-3">
              <motion.path
                d="M3.5 8.4 6.6 11.4 12.5 4.8"
                className="fill-none stroke-void-950"
                strokeWidth={2.4}
                strokeLinecap="round"
                strokeLinejoin="round"
                initial={{ pathLength: 0 }}
                animate={{ pathLength: 1 }}
                transition={{ duration: 0.22, ease: "easeOut" }}
              />
            </svg>
          )}
        </span>
        <span className={cn("text-sm text-steel-100 transition-colors", done && "text-steel-500 line-through")}>
          {todo.text}
        </span>
      </label>
      <IconButton
        label={`Drop “${todo.text}” without doing it`}
        tip={false}
        size="xs"
        disabled={done || !online}
        onClick={onDrop}
        className="mt-1 opacity-0 transition-opacity group-hover/task:opacity-100 focus-visible:opacity-100 max-desk:opacity-100"
        data-testid="task-drop"
      >
        <Trash2 />
      </IconButton>
    </div>
  )
}
