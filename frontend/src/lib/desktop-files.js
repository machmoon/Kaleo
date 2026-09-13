import { save } from '@tauri-apps/plugin-dialog'
import { writeTextFile } from '@tauri-apps/plugin-fs'

/** Save text through a user-selected native path. Cancellation is not an error. */
export async function saveTextWithDialog(
  text,
  filename,
  { choosePath = save, writeText = writeTextFile } = {},
) {
  const path = await choosePath({ defaultPath: filename })
  if (!path) return ''
  await writeText(path, text)
  return path
}
