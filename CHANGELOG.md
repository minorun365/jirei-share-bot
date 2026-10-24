# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-10-24

### Added

- Intent classification before tool selection; write tools only write when the classified intent matches with confidence of 0.7 or higher
- Append-target selection by the agent via `case_id`, with numbered candidates and a pending-content handoff when the target is ambiguous
- Revision history table for cases and a revert operation (`revert_case_tool`)
- Merging appended content with the previous version instead of replacing fields; titles change only on explicit rename requests
- Guards against writing another customer's content into a case and against updating more than one case per request
- Replies to thread messages without a mention when the previous message is from the bot (`message.channels` subscription)
- Skipping the daily share on Japanese national holidays and the year-end holidays (Dec 29 to Jan 3)
- Failure notifications that mention the developer set in `ownerSlackUserId`, in the thread or in the target channel for daily-share failures and queue alarms
- `schedulesEnabled` context flag to deploy all schedules in a disabled state
- Intent evaluation set with fictional cases (`eval/`) and a revision-history canary

### Changed

- Registration requests that are explicitly marked as separate cases are registered without a confirmation question
- The bot no longer repeats the case-name lead or re-attaches the same graphic within a thread

### Removed

- Regex- and score-based selection of the update target

## [0.1.0] - 2026-08-13

### Added

- Slack mentions for registering, updating, and searching internal case studies
- Asynchronous processing with API Gateway, Lambda, SQS, and AgentCore Runtime
- DynamoDB as the source of truth and semantic search with Knowledge Bases and S3 Vectors
- Scheduled Slack sharing and knowledge-base synchronization
- Optional integrations for URL retrieval, public web search, Google Drive, and image generation
- Public-source scanning, dependency audits, and automated tests
