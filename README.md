# Didimlog

English | [한국어](README.ko.md)

Didimlog is a CLI that stores **lessons, observations, experiments, and evidence** gathered while working with Claude Code in local files, then retrieves only what is needed for the next task.

It is well suited for the following use cases:

- Preserve verified lessons by project so you do not solve the same problem twice.
- Link experiment results and raw artifacts to a Git project for traceability.
- Load only relevant material instead of placing the entire knowledge base in the AI context.
- Use a local-first storage model that never overwrites source content.

The project is currently **Pre-Alpha**. It supports macOS and Linux with Python 3.11–3.14. Windows has not yet been verified.

## Requirements

- macOS or Linux
- Python 3.11–3.14
- [`uv`](https://docs.astral.sh/uv/), the recommended installation tool
- A Git repository when storing project records
- A Claude configuration directory created by running Claude Code at least once when integrating with Claude Code (default: `~/.claude`)

If you do not use Claude Code or have not configured it yet, you can use `--skip-claude` during initial setup.

## Quick Start

This walkthrough installs Didimlog, prepares storage in a Git project, saves the first lesson, and checks the index status.

### 1. Install

```sh
uv tool install didimlog
didim --version
```

If you use `pipx`, install it with `pipx install didimlog`.

### 2. Review the Change Plan and Set Up

Run these commands from the top-level directory of the target Git project.

```sh
cd /path/to/your-project
didim setup --dry-run
didim setup --yes
```

`--dry-run` shows the planned changes to personal knowledge, project evidence, and the Claude integration without modifying any files. When the actual setup finishes, the final line prints:

```text
Didimlog 준비를 마쳤습니다.
```

If you run the same command again, items that are already prepared are shown as `변경 없음`.

### 3. Check Readiness

```sh
didim status
```

If setup completed successfully, you can verify the following status. The project name is the name of the current Git top-level directory.

```text
개인 지식: 최신
현재 프로젝트: <프로젝트 이름>
프로젝트 근거: 최신
Claude 연결: 정상
```

### 4. Save Your First Lesson

Lessons accept Markdown source through standard input. The following example includes the execution time in the slug, so repeated runs do not overwrite an existing lesson.

```sh
today="$(date +%F)"
slug="didimlog-quick-start-$(date +%Y%m%d-%H%M%S)"
cat > /tmp/didimlog-quick-start.md <<EOF
---
topic: didimlog-quick-start
title: 같은 문제를 다시 풀지 않는다
summary: 검증한 해결 방법을 저장하고 다음 작업에서 다시 찾는다
tags: [didimlog, quick-start]
date: $today
---
## 상황
반복되는 작업에서 이미 검증한 해결 방법이 필요했다.

## 교훈
작업이 끝난 뒤 재사용할 조건과 절차를 교훈으로 저장한다.

## 근거
Didimlog로 교훈을 저장하고 index 상태를 확인했다.
EOF

didim add lesson "$slug" --date "$today" < /tmp/didimlog-quick-start.md
didim index --check
```

On success, Didimlog prints the lesson path and the current status of both indexes.

```text
lessons/<프로젝트 이름>/didimlog-quick-start-<실행 시각>.md
개인 지식: PERSONAL_INDEX_CURRENT
프로젝트 근거: PROJECT_INDEX_CURRENT
```

The lesson source now remains in `~/knowledge/lessons/<프로젝트 이름>/`. During the next task, Claude Code searches the index first and reads only the relevant source content.

## Next Steps

- If your team needs to share records from the same project through Git, see [Share Project Knowledge with the Team](#share-project-knowledge-with-the-team).
- To record facts you have directly verified, see [Record a Project Observation](#record-a-project-observation).
- To record a hypothesis together with its result, see [Record Experiment Results](#record-experiment-results).
- To bundle a file or Git source in a verifiable form, see [Register Evidence](#register-evidence).
- If the status differs from what you expect, see [Diagnose Problems](#diagnose-problems).

## Common Tasks

### Share Project Knowledge with the Team

By default, the project's `knowledge/` directory is used only on this computer. To include it in Git, reapply the setup with the following command.

```sh
didim setup --yes --project-knowledge shared
```

`shared` removes only the Didimlog-managed block from the local Git exclude file. It does not modify `.gitignore`, global exclude settings, or other user-defined exclusion rules. If the command reports that exclusion rules remain, you must inspect those rules yourself.

To switch back to local-only storage, use:

```sh
didim setup --yes --project-knowledge local
```

### Record a Project Observation

An observation is a reusable fact that you have directly verified. Include only `body` in the JSON body.

```sh
today="$(date +%F)"
printf '%s' '{"body":"setup 뒤 status의 네 항목이 모두 정상 또는 최신으로 표시됐다."}' |
  didim add observation \
    --date "$today" \
    --title "초기 설정 상태 확인" \
    --tags "setup,status"
```

On success, Didimlog assigns an ID and prints a path in the following form.

```text
<git-root>/knowledge/records/observation/OBS-YYYYMMDD-NN.md
```

### Record Experiment Results

An experiment stores the hypothesis, method, result, contradiction signal, and interpretation together. `result` must be one of `success`, `failure`, or `inconclusive`. Set `contradicts` to `none` when there is no contradiction.

```sh
today="$(date +%F)"
printf '%s' '{"hypothesis":"index를 다시 만들면 저장 직후 상태를 유지한다.","method":"didim index를 실행한 뒤 didim index --check를 실행했다.","result":"success","contradicts":"none","interpretation":"두 index가 최신이므로 현재 기록 트리와 일치한다."}' |
  didim add experiment \
    --date "$today" \
    --title "index 재생성 확인" \
    --tags "index"
```

On success, Didimlog prints a path in the following form.

```text
<git-root>/knowledge/records/experiment/EXP-YYYYMMDD-NN.md
```

### Register Evidence

To register a local file as evidence, first create it under `knowledge/raw/` and submit its SHA-256 digest. The following example uses a unique filename.

```sh
today="$(date +%F)"
artifact="knowledge/raw/setup-status-$(date +%Y%m%d-%H%M%S).txt"
printf 'setup status: current\n' > "$artifact"
digest="$(python3 -c 'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' "$artifact")"
printf '{"artifact":"%s","origin":"didim status output","collection":"captured after setup","artifact_sha256":"%s"}' "$artifact" "$digest" |
  didim add evidence \
    --date "$today" \
    --title "설정 상태 원본" \
    --tags "setup,status"
```

On success, Didimlog prints a path in the following form.

```text
<git-root>/knowledge/records/evidence/EVD-YYYYMMDD-NN.md
```

For an artifact included in a Git commit, put the full commit object ID in `artifact_git` instead of `artifact_sha256`. For path constraints, Git verification behavior, and the record lifecycle, refer to the installed copy of [`knowledge/README.md`](src/didimlog/resources/project/README.md) created during setup.

### Rebuild the Indexes

Rebuild the indexes for all personal knowledge and the current Git project.

```sh
didim index
```

The personal index treats only these Markdown paths as source content:

```text
lessons/<project>/*.md
docs/<project>/**/*.md
book/<project>/*.md
```

Entries outside these patterns, such as `.DS_Store`, images, and editor temporary files, are ignored. Each project directory directly below `lessons/`, `docs/`, or `book/` may be a single symlink to an external directory.

```text
lessons/my-project -> /path/to/external-lessons
```

Symlinks for individual Markdown files or nested project directories are rejected. Indexes and CLI output use logical paths such as `lessons/my-project/...` even when the source is external. If a source is invalid, `didim --explain-errors index` shows the logical path under `무엇:` and the cause under `이유:`.

To verify that the source content and indexes match without changing files, use:

```sh
didim index --check
```

When both indexes are current, the command returns exit `0` and the following tokens.

```text
개인 지식: PERSONAL_INDEX_CURRENT
프로젝트 근거: PROJECT_INDEX_CURRENT
```

### Diagnose Problems

```sh
didim status
didim doctor
```

`status` summarizes the version, personal knowledge, current project, project evidence, and Claude integration. `doctor` shows the impact of each detected problem together with the next command to run.

If you also need error explanations in automation logs, place the global option before the command.

```sh
didim --explain-errors index --check
```

## Command Summary

The following table summarizes commands intended to be run directly by users. See `didim <command> --help` in the installed version for every available option.

| Command | Result | Main options or input |
| --- | --- | --- |
| `didim setup` | Prepare personal and project storage and the Claude integration | `--dry-run`, `--yes`, `--skip-claude`, `--project-knowledge local\|shared`, `--config-dir` |
| `didim connect claude` | Add the Claude Code integration | `--yes`, `--config-dir` |
| `didim disconnect claude` | Remove the Claude integration managed by Didimlog | `--config-dir` |
| `didim add lesson <slug>` | Save a personal lesson as create-only | Markdown stdin, `--date`, `--project`, `--global` |
| `didim add observation` | Save a project observation record | JSON stdin, common record options |
| `didim add experiment` | Save a project experiment record | JSON stdin, common record options |
| `didim add evidence` | Link project evidence with its artifact | JSON stdin, common record options |
| `didim index` | Rebuild personal and project indexes | `--check` |
| `didim status` | Summarize the current status | `--config-dir` |
| `didim doctor` | Diagnose problems and remediation steps | `--config-dir` |

The global options are `--version` and `--explain-errors`. `didim hook session-start` is an internal command used by the Claude Code integration.

### Common Record Options

`observation`, `experiment`, and `evidence` share the following options.

| Option | Meaning |
| --- | --- |
| `--date YYYY-MM-DD` | Creation date. Required for non-interactive execution using standard input |
| `--title` | Record title. Required |
| `--scope` | `project` or `task:<name>`. Default: `project` |
| `--tags` | Comma-separated tags |
| `--sources` | Comma-separated EVD or EXP IDs |

The exact fields accepted through JSON stdin are listed below. Non-string values and unknown fields are rejected.

| Type | Required fields |
| --- | --- |
| observation | `body` |
| experiment | `hypothesis`, `method`, `result`, `contradicts`, `interpretation` |
| evidence | `artifact`, `origin`, `collection`, and exactly one of `artifact_sha256` or `artifact_git` |

CLI standard input is limited to 64 KiB. Generated project records are limited to 12,000 UTF-8 bytes and 200 LF characters. For the authoritative format and validation rules, refer to `knowledge/README.md` and `knowledge/schema/record.schema.json` in the installed project.

### Errors and Exit Codes

The English token on the first error line and the exit code are stable behaviors that can be used in automation. In an interactive terminal or when using `--explain-errors`, a Korean `도움말:` line is appended.

| Exit code | Meaning |
| --- | --- |
| `2` | Command usage or input format error |
| `3` | Violation of a knowledge integrity policy involving a path, artifact, index, or similar input |
| `5` | Content resembling a secret was detected in a lesson |
| `7` | The Git repository or object cannot be verified |

`LESSON_SECRET` rejects the lesson without printing the detected value. `KNOWLEDGE_INDEX_STALE` or `PROJECT_INDEX_STALE` means that the source content was saved successfully but the index update failed. Recover by running `didim index`.

## Storage Model and Safety Rules

Personal knowledge is stored in the home directory.

```text
~/knowledge/
├── lessons/<project>/
├── docs/<project>/
├── book/<project>/
├── lessons/_global/
├── docs/_global/
├── book/_global/
└── index/<project>.md
```

- `lessons`: Verified lessons that should change behavior in future tasks
- `docs`: Precise procedures, plans, and research results
- `book`: Explanations that connect background information and examples from multiple sources
- `_global`: Material that applies unchanged across multiple projects
- `index`: A regenerable list containing only titles, when-to-use guidance, and detailed file paths

Project knowledge is stored at the top level of the Git repository.

```text
<git-root>/knowledge/
├── POINTER.md
├── README.md
├── records/
├── raw/
├── schema/record.schema.json
├── index/INDEX.md
└── active/harness.md
```

`records/` is the source of truth for project knowledge, and `INDEX.md` can be regenerated by revalidating every record. The default `local` configuration adds `/knowledge/` to the current local repository's `info/exclude` without modifying `.gitignore`. Linked worktrees share this file, so configuration in one worktree also applies to other linked worktrees.

Didimlog enforces the following safety rules:

- Lesson and record source files are create-only. Existing source files are never overwritten or deleted.
- Before setup, Didimlog checks the complete change plan. If a path or file changes after approval, it stops writing.
- Symlinks and parent-path traversal are rejected.
- If setup fails, Didimlog rolls back only content created by the current run that remains unchanged. Concurrent user changes are not modified.
- If only the index update fails after creating a record file, Didimlog preserves the source content and provides a recovery command.
- Evidence backed by Git objects is verified only against the object database of the repository identified when the command starts. Didimlog does not use the current shell's Git environment variables or external alternates to locate other objects.

## Claude Code Integration

`didim setup --yes` prepares both storage and the Claude Code integration. To prepare storage only, use:

```sh
didim setup --yes --skip-claude
```

You can also manage the integration separately.

```sh
didim connect claude --yes
didim disconnect claude
```

A newly connected Claude session automatically reads only the short retrieval procedure in `KNOWLEDGE_USAGE.md`. During actual work, it searches the current project and `_global` indexes and reads up to five relevant source documents. It does not load the complete indexes or every lesson body into the session-start context.

## Uninstallation

First disconnect Claude Code, then uninstall the tool.

```sh
didim disconnect claude
uv tool uninstall didimlog
```

Uninstallation leaves `~/knowledge` and the source content under each project's `knowledge/` directory intact.

## Development and Verification

Install locked dependencies and run the full test suite.

```sh
uv sync --locked
uv run --project . python -m unittest discover -s tests -v
```

Verify the distribution files and public allowlist.

```sh
uv build
uv run --project . python -m unittest tests.didimlog_tests.test_release -v
```

### Release

Before using release automation, a repository administrator must apply the following settings manually. Merging the workflow file alone does not create these settings.

- Set GitHub Actions `Workflow permissions` to `Read and write permissions`.
- Create the `release:none`, `release:patch`, `release:minor`, `release:major`, and `release:ready` labels.
- Configure `main` to require PRs, CI, and `release-state`, and require PR branches to include the latest `main` before they can be merged. Allow only merge commits, and block squash merges, rebases, and direct pushes.
- Allow GitHub Actions to push preparation and cancellation commits to `develop`.
- Create the `pypi` environment, and do not assign required reviewers if fully automated deployment is desired.
- Register a PyPI Trusted Publisher with owner `zhsks311`, repository `didimlog`, workflow `release.yml`, and environment `pypi`.
- Enable GitHub Release immutability so releases cannot be modified after publication.

A `develop` → `main` PR is not released if it has no release label or has `release:none`. To release it, apply exactly one of the following version labels:

- `release:patch`: Prepare a bug-fix release
- `release:minor`: Prepare a backward-compatible feature release
- `release:major`: Prepare a release with breaking changes

The automation updates `pyproject.toml`, `uv.lock`, and `CHANGELOG.md`, then runs the full test suite and build. When preparation finishes, it applies `release:ready`, but this label is only an indicator of the current state. The actual required merge condition is that the Git history of the current PR commit contains a valid preparation record and the `release-state` check passes for that exact commit.

If a commit is added to the PR after preparation, the automation cancels the previous preparation and prepares again from the new commit. If `main` advances in the meantime, it cancels the previous preparation and waits until the PR branch includes the latest `main`. It then prepares the release again automatically.

Only a merge commit on `main` with two parents is recognized as a release target. Squash merges, rebases, and direct pushes are not released and cause the release workflow to finish with an error.

If deployment fails, select `Run workflow` in GitHub Actions under `Publish prepared main release`, then enter the failed merge commit SHA. The automation verifies that the commit is still a valid release contained in the current `main`, and resumes publication of the same version only when it does not conflict with existing tags or files.

A `hotfix/*` → `main` PR supports only `release:patch`. After a successful patch release, the automation merges `main` directly into `develop`. If protection rules or conflicts prevent the direct update, it creates or updates a `main` → `develop` synchronization PR.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for contribution instructions, [`SECURITY.md`](SECURITY.md) for reporting security issues, and [`CHANGELOG.md`](CHANGELOG.md) for user-visible changes.

## License

Didimlog is distributed under the [MIT License](LICENSE). Licenses for Python-Markdown and the vendored Mermaid are listed in [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
