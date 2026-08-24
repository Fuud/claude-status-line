#!/usr/bin/env bash
input=$(cat)

session_id=$(echo "$input" | jq -r '.session_id // empty')

branch=$(git --no-optional-locks branch --show-current 2>/dev/null || echo 'no-branch')
model=$(echo "$input" | jq -r '.model.display_name // empty')
user="${AI_USER:-n/a}"

prompt_id=$(echo "$input" | jq -r '.prompt_id // empty')
current_usage=$(echo "$input" | jq -r '.context_window.current_usage // empty')
used_pct=$(echo "$input" | jq -r '.context_window.used_percentage // 0')
total_input_tokens=$(echo "$input" | jq -r '.context_window.total_input_tokens // 0')

# Context size in thousands of tokens, rounded to nearest integer
ctx_k=$(( (total_input_tokens + 500) / 1000 ))
[ "$ctx_k" -lt 0 ] && ctx_k=0

# Temp file per session for cumulative totals
cum_file="/tmp/claude_status_cum_${session_id}.json"

if [ -n "$current_usage" ] && [ "$current_usage" != "null" ]; then
  cur_in=$(echo "$input" | jq -r '.context_window.current_usage.input_tokens // 0')
  cur_out=$(echo "$input" | jq -r '.context_window.current_usage.output_tokens // 0')
  cur_cache=$(echo "$input" | jq -r '.context_window.current_usage.cache_read_input_tokens // 0')

  if [ -f "$cum_file" ]; then
    last_prompt=$(jq -r '.last_prompt_id // empty' "$cum_file" 2>/dev/null)
  else
    last_prompt=""
  fi

  if [ "$prompt_id" != "$last_prompt" ]; then
    # New API response — accumulate
    if [ -f "$cum_file" ]; then
      prev_in=$(jq -r '.cum_in // 0' "$cum_file" 2>/dev/null)
      prev_out=$(jq -r '.cum_out // 0' "$cum_file" 2>/dev/null)
      prev_cache=$(jq -r '.cum_cache // 0' "$cum_file" 2>/dev/null)
    else
      prev_in=0
      prev_out=0
      prev_cache=0
    fi

    cum_in=$((prev_in + cur_in))
    cum_out=$((prev_out + cur_out))
    cum_cache=$((prev_cache + cur_cache))

    jq -n \
      --arg pid "$prompt_id" \
      --argjson ci "$cum_in" \
      --argjson co "$cum_out" \
      --argjson cc "$cum_cache" \
      '{last_prompt_id: $pid, cum_in: $ci, cum_out: $co, cum_cache: $cc}' \
      > "$cum_file"
  else
    # Same prompt_id — just read stored totals
    cum_in=$(jq -r '.cum_in // 0' "$cum_file" 2>/dev/null)
    cum_out=$(jq -r '.cum_out // 0' "$cum_file" 2>/dev/null)
    cum_cache=$(jq -r '.cum_cache // 0' "$cum_file" 2>/dev/null)
  fi
else
  # current_usage is null (before first API call or after /compact)
  if [ -f "$cum_file" ]; then
    cum_in=$(jq -r '.cum_in // 0' "$cum_file" 2>/dev/null)
    cum_out=$(jq -r '.cum_out // 0' "$cum_file" 2>/dev/null)
    cum_cache=$(jq -r '.cum_cache // 0' "$cum_file" 2>/dev/null)
  else
    cum_in=0
    cum_out=0
    cum_cache=0
  fi
fi

printf "Session: %s | Branch: %s | Model: %s | User: %s | Cum: %sin %sout %scache | Context: %sK (%s%%)" \
  "$session_id" "$branch" "$model" "$user" "$cum_in" "$cum_out" "$cum_cache" "$ctx_k" "$used_pct"
