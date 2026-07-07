#!/usr/bin/env bash
# Fetch PR review threads with resolved/outdated status via GraphQL
# Usage: gh-pr-comments.sh <owner/repo> <pr_number>

REPO="$1"
PR="$2"

if [ -z "$REPO" ] || [ -z "$PR" ]; then
  echo "Usage: $0 <owner/repo> <pr_number>" >&2
  exit 1
fi

OWNER="${REPO%%/*}"
NAME="${REPO##*/}"

echo "=== Review threads ==="
gh api graphql --paginate -f query='
query($owner: String!, $name: String!, $pr: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $pr) {
      reviewThreads(first: 50, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          isResolved
          isOutdated
          comments(first: 20) {
            nodes {
              author { login }
              body
              path
              createdAt
            }
          }
        }
      }
    }
  }
}' -f owner="$OWNER" -f name="$NAME" -F pr="$PR" \
  --jq '.data.repository.pullRequest.reviewThreads.nodes[] |
    select(.comments.nodes[0].author.login | test("\\[bot\\]$") | not) |
    {
      resolved: .isResolved,
      outdated: .isOutdated,
      path: .comments.nodes[0].path,
      started_by: .comments.nodes[0].author.login,
      started_at: .comments.nodes[0].createdAt,
      first_comment: (.comments.nodes[0].body[:200]),
      reply_count: ((.comments.nodes | length) - 1),
      last_reply_by: (if (.comments.nodes | length) > 1 then .comments.nodes[-1].author.login else null end),
      last_reply_at: (if (.comments.nodes | length) > 1 then .comments.nodes[-1].createdAt else null end)
    }'

echo ""
echo "=== General comments ==="
gh api "repos/$REPO/issues/$PR/comments" --jq '[.[] | select(.user.login | test("\\[bot\\]$") | not) | {user: .user.login, body: .body[:200], created_at: .created_at}]'
