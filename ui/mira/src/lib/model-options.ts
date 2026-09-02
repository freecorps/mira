export type ModelOption = {
  value: string
  label: string
  recommended?: boolean
  // Which backend serves this option. The same model name can appear under
  // two groups (an API key's proxy and a signed-in account, say), and the
  // group is what tells them apart — in the list and in the selected value.
  group?: string
  // Protocol and endpoint, one line under the group header.
  detail?: string
  description?: string
}

// What the closed picker shows for a value: the option's label, with its
// group when the catalog has more than one, so "GPT-5 Codex" reads as
// "GPT-5 Codex · ChatGPT (Codex) · you@example.com" rather than one of
// several identical names.
export function describeSelection(
  value: string,
  options: ModelOption[]
): string {
  const option = options.find((o) => o.value === value)
  if (!option) return value
  const groups = new Set(options.map((o) => o.group).filter(Boolean))
  return option.group && groups.size > 1
    ? `${option.label} · ${option.group}`
    : option.label
}
