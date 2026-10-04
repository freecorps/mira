// API client. The methods are organized into per-domain modules under
// `lib/api/`; this file composes them into the single `api` object and
// re-exports the shared types, so `import { api, SomeType } from "@/lib/api"`
// keeps working everywhere.

import { activityApi } from "./api/activity"
import { analyticsApi } from "./api/analytics"
import { autofixApi } from "./api/autofix"
import { checksApi } from "./api/checks"
import { contributorsApi } from "./api/contributors"
import { digestsApi } from "./api/digests"
import { deliveryApi } from "./api/delivery"
import { gateApi } from "./api/gate"
import { logsApi } from "./api/logs"
import { oauthApi } from "./api/oauth"
import { packagesApi } from "./api/packages"
import { providersApi } from "./api/providers"
import { qualityApi } from "./api/quality"
import { relationshipsApi } from "./api/relationships"
import { reposApi } from "./api/repos"
import { reviewInsightsApi } from "./api/review-insights"
import { rulesApi } from "./api/rules"
import { settingsApi } from "./api/settings"
import { statsApi } from "./api/stats"
import { systemApi } from "./api/system"
import { tokensApi } from "./api/tokens"
import { triageApi } from "./api/triage"
import { usersApi } from "./api/users"
import { vulnerabilitiesApi } from "./api/vulnerabilities"
import { webhooksApi } from "./api/webhooks"

export * from "./api/types"
export type * from "./api/delivery"
export type { ApiToken, CreatedApiToken } from "./api/tokens"
export type {
  DigestArea,
  DigestChange,
  DigestDetail,
  DigestListItem,
} from "./api/digests"

export const api = {
  ...activityApi,
  ...analyticsApi,
  ...systemApi,
  ...settingsApi,
  ...statsApi,
  ...reposApi,
  ...packagesApi,
  ...vulnerabilitiesApi,
  ...relationshipsApi,
  ...rulesApi,
  ...usersApi,
  ...tokensApi,
  ...webhooksApi,
  ...contributorsApi,
  ...digestsApi,
  ...reviewInsightsApi,
  ...deliveryApi,
  ...gateApi,
  ...autofixApi,
  ...checksApi,
  ...logsApi,
  ...oauthApi,
  ...providersApi,
  ...qualityApi,
  ...triageApi,
}
