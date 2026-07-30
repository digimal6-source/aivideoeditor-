# Fonts

This directory is where the app looks for `.ttf` / `.otf` files at runtime.

**No font binaries are committed to this repository.** Indivisible and Rubik are
not ours to redistribute, and `.gitignore` deliberately excludes everything in
this folder except this README.

## Two ways to install a font

### 1. Upload it in the app (easiest, works from a phone)

Open the app -> **Fonts** section -> *Upload font* -> pick a `.ttf` or `.otf`.
The file is validated and saved into this directory, then appears in the hook
and caption font dropdowns immediately.

### 2. Drop the file in this folder

In the Codespaces file explorer, drag your font file into `fonts/`. Then reload
the app page.

## The two fonts this workflow expects

| Font | Used for | Where to get it |
| --- | --- | --- |
| **Indivisible** | Captions | Commercial font - use the `.ttf`/`.otf` you already licensed |
| **Rubik Bold** | On-screen hook | Free, SIL Open Font License - <https://fonts.google.com/specimen/Rubik> |

## What happens if a font is missing

The render **does not fail** and the font is **not silently swapped**. The job
completes using a fallback sans-serif and returns an explicit warning such as:

> Indivisible font is not installed, so the captions were rendered with DejaVu Sans
> instead. Upload Indivisible (.ttf or .otf) in the Fonts section to get the exact styling.

The warning is shown in the UI next to the finished video.

## Naming

A font's id is derived from its filename (`Rubik-Bold.ttf` -> `rubik-bold`), and
its display name comes from the family name inside the font file. To have a font
auto-selected by the built-in **My Default** preset, name the files:

```
fonts/Indivisible.ttf     ->  id: indivisible
fonts/Rubik-Bold.ttf      ->  id: rubik-bold
```
