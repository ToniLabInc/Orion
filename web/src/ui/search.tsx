/**
 * A search field: a text field with a magnifier in front and, once something is typed, a clear button (Esc clears it
 * too). The value is the owner's; asking for results after the typing settles is the owner's too (`useSettled`).
 */
import { Search, X } from "lucide-react"
import type { ComponentProps } from "react"
import { cn } from "@/lib/utils"
import { IconButton } from "./button"
import { Input } from "./input"

/**
 * The field.
 * @param props the `value`, `onChange` (with the new text), an aria `label`, and an input's props
 * @returns the field
 */
export function SearchField({
  value,
  onChange,
  label,
  className,
  ...props
}: Omit<ComponentProps<"input">, "value" | "onChange" | "type"> & {
  value: string
  onChange: (value: string) => void
  label: string
}) {
  return (
    <div className={cn("relative", className)}>
      <Search aria-hidden="true" className="pointer-events-none absolute top-2.5 left-3 size-4 text-steel-500" />
      <Input
        type="search"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Escape" && value) {
            e.preventDefault()
            e.stopPropagation()
            onChange("")
          }
        }}
        aria-label={label}
        autoComplete="off"
        spellCheck={false}
        className="pr-9 pl-9 [&::-webkit-search-cancel-button]:appearance-none"
        {...props}
      />
      {value && (
        <IconButton
          label="Clear"
          tip={false}
          size="xs"
          className="absolute top-1 right-1.5"
          onClick={() => onChange("")}
        >
          <X />
        </IconButton>
      )}
    </div>
  )
}
