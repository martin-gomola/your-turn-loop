---
name: codex-image
metadata:
  version: "0.3.0"
description: Generates bitmap images (app icons, illustrations, social previews, hero images, mockups, backgrounds) when the current agent has no image-generation tool, by delegating to a local Codex CLI that can generate images. Use from Claude Code, Cursor, Gemini, Antigravity or any non-Codex agent asked to create, draw or generate an image, even when the request never mentions Codex; avoid for prompt-only work, crops or resizes, or when a native image tool already fits.
---

# Codex Image Handoff

Use this workflow when an agent without its own image-generation tool, such as Claude Code, Cursor, Gemini or Antigravity, is asked to create an image, and a local Codex CLI is available to generate it. The request does not have to name Codex: "make an app icon" or "generate a hero image" is enough when the current agent cannot produce the image itself. This skill does not provide an image-generation tool; Codex must produce an actual image file for the handoff to succeed.

## Workflow

1. Confirm the user wants an image generated now. If they only want a prompt, plan, critique, or skill saved, do not launch Codex.
2. Resolve the image's purpose, target size/aspect ratio, format, exact text, visual direction, and final path from the request and project context. Ask only for missing details that materially change the result.
3. Check `codex --version` and `codex exec --help`. Use only options the installed CLI reports. The `--image`/`-i` option attaches reference images; its presence does not establish that this Codex runtime can generate images. Ask the child Codex to use its image-generation tool and stop if it cannot or returns only text. If `type -a codex` shows a shell function or alias wrapping the CLI, for example one that adds `--yolo` or other flags, call `command codex` or the binary path from `which -a codex` in step 7, so the wrapper cannot widen access or break the flag order.
4. Create a task-specific working directory that is ignored by Git, or use a temporary directory outside the repository. Keep the prompt, copied/cropped references, log, and output there. Never overwrite source images or unrelated files.
5. Prepare a concise `prompt.txt`. Lead with a positive scene description: subject, composition, lighting, style, background and intended use. Name each reference file and its role. State exact text in quotes, target dimensions and format, and safe margins. Retain explicit exclusions from the user's brief when needed, rather than adding a generic ban list. Ask for the final image at the requested path and prohibit edits to other files. Do not promise exact rendered lettering; verify it in the result.
6. Attach only the references needed for this image. Do not include credentials, auth files, private prompts, or unrelated project data. If a reference contains sensitive or confidential material and the user has not authorized sharing it with the configured Codex service, ask before attaching it.
7. Run `codex exec` with the task directory as its working root and the least access needed. For a CLI whose help shows these flags, the pattern is:

   ```sh
   codex exec --sandbox workspace-write -C . \
     --image reference-1.png --image reference-2.png - \
     < prompt.txt > codex.log 2>&1
   ```

   Omit repeated `--image` flags when there are no references. `-` reads the request from stdin. Invoke `command codex` or the resolved binary path from step 3 rather than a shell wrapper. When the task directory is a temporary directory outside a Git repository (step 4), add `--skip-git-repo-check` if `codex exec --help` lists it. Bound every run with a watchdog, such as a `perl -e 'alarm shift; exec @ARGV' 600` prefix or `timeout 600` where installed, and treat a timeout as a failed run. Do not use `--dangerously-bypass-approvals-and-sandbox`, `--dangerously-bypass-hook-trust`, or broader filesystem access just to make image generation work. If the runtime lacks the image tool or needed permissions, stop and explain the missing capability; do not silently switch to a different service or API.
8. Verify that the requested output exists, opens as an image, has the requested dimensions and format, and contains the right scene and spelling. Inspect it visually. If it misses the brief, make at most one focused correction within the authorized request; if it still fails, preserve the result and ask for user feedback.
9. Return the output path and a short verification summary. Keep generated files and logs out of Git unless the user asks to add them. Preserve the final image for the user; do not delete it during cleanup.

## Source and capability boundary

Its historical local handoff is not bundled as runtime evidence. Recheck CLI options and image-generation availability on each machine. A Codex CLI image-input flag is not proof that the child runtime exposes image generation. The workflow requires an existing Codex installation and its already-configured authentication; it installs nothing and does not request or print credentials.

This skill is for handing off an image task from a non-Codex agent to Codex. Before telling the user an image cannot be made here, check for a local `codex` CLI and use this workflow. Use the host's native image-generation capability when it is directly available and the user has not specifically asked for Codex delegation. Use a focused style skill, such as `travel-poster`, when its stored art direction is the user's intent.
