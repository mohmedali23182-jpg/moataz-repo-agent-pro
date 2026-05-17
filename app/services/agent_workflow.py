from __future__ import annotations

AGENT_WORKFLOW_PATH = '.github/workflows/agent-command.yml'
AGENT_WORKFLOW_ID = 'agent-command.yml'

AGENT_WORKFLOW_CONTENT = '''name: Agent Command

on:
  workflow_dispatch:
    inputs:
      command:
        description: "Shell command to run"
        required: true
        type: string
      workdir:
        description: "Working directory"
        required: false
        default: "."
        type: string
      commit_changes:
        description: "Commit changed files after command finishes"
        required: false
        default: "false"
        type: choice
        options:
          - "false"
          - "true"
      commit_message:
        description: "Commit message when commit_changes=true"
        required: false
        default: "Apply agent terminal changes"
        type: string

permissions:
  contents: write
  actions: read

jobs:
  command:
    runs-on: ubuntu-latest
    timeout-minutes: 20

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Show context
        shell: bash
        run: |
          set -euo pipefail
          echo "Repository: $GITHUB_REPOSITORY"
          echo "Ref: $GITHUB_REF"
          echo "Actor: $GITHUB_ACTOR"
          pwd
          ls -la
          echo "Workdir: ${{ inputs.workdir }}"

      - name: Run agent command
        shell: bash
        env:
          AGENT_COMMAND: ${{ inputs.command }}
        run: |
          set -euo pipefail
          cd "${{ inputs.workdir }}"
          echo "Running command:"
          printf '%s\\n' "$AGENT_COMMAND"
          bash -lc "$AGENT_COMMAND"

      - name: Show git status
        if: always()
        shell: bash
        run: |
          git status --short || true

      - name: Commit changes
        if: ${{ inputs.commit_changes == 'true' }}
        shell: bash
        run: |
          set -euo pipefail
          if [[ -n "$(git status --porcelain)" ]]; then
            git config user.name "Moataz Repo Agent"
            git config user.email "actions@github.com"
            git add -A
            git commit -m "${{ inputs.commit_message }}"
            git push
          else
            echo "No changes to commit."
          fi
'''
