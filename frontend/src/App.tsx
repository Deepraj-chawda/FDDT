import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { lazy, Suspense } from "react"
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom"

import { RoleRoute } from "@/components/AdminRoute"
import { ProtectedRoute } from "@/components/ProtectedRoute"
import { ActingCompanyProvider } from "@/hooks/useActingCompany"
import { AuthProvider } from "@/hooks/useAuth"
import { LoginPage } from "@/pages/LoginPage"

// Route-level code splitting: each page (and the libraries only it uses) is
// its own chunk, fetched the first time the route is opened. The login page
// stays in the main bundle since it is the first screen.
const AuditHistoryPage = lazy(() => import("@/pages/AuditHistoryPage").then((m) => ({ default: m.AuditHistoryPage })))
const BulkUploadDetailPage = lazy(() => import("@/pages/BulkUploadDetailPage").then((m) => ({ default: m.BulkUploadDetailPage })))
const BulkUploadsListPage = lazy(() => import("@/pages/BulkUploadsListPage").then((m) => ({ default: m.BulkUploadsListPage })))
const BulkUploadPage = lazy(() => import("@/pages/BulkUploadPage").then((m) => ({ default: m.BulkUploadPage })))
const CaseDetailPage = lazy(() => import("@/pages/CaseDetailPage").then((m) => ({ default: m.CaseDetailPage })))
const CaseQueuePage = lazy(() => import("@/pages/CaseQueuePage").then((m) => ({ default: m.CaseQueuePage })))
const DashboardPage = lazy(() => import("@/pages/DashboardPage").then((m) => ({ default: m.DashboardPage })))
const MyCasesPage = lazy(() => import("@/pages/MyCasesPage").then((m) => ({ default: m.MyCasesPage })))
const NewCasePage = lazy(() => import("@/pages/NewCasePage").then((m) => ({ default: m.NewCasePage })))
const PlatformCompaniesPage = lazy(() => import("@/pages/PlatformCompaniesPage").then((m) => ({ default: m.PlatformCompaniesPage })))
const PlatformQueuesPage = lazy(() => import("@/pages/PlatformQueuesPage").then((m) => ({ default: m.PlatformQueuesPage })))
const PlatformRuleTemplatesPage = lazy(() => import("@/pages/PlatformRuleTemplatesPage").then((m) => ({ default: m.PlatformRuleTemplatesPage })))
const PlatformUsagePage = lazy(() => import("@/pages/PlatformUsagePage").then((m) => ({ default: m.PlatformUsagePage })))
const SettingsIssuerRegistryPage = lazy(() => import("@/pages/SettingsIssuerRegistryPage").then((m) => ({ default: m.SettingsIssuerRegistryPage })))
const SettingsRiskRulesPage = lazy(() => import("@/pages/SettingsRiskRulesPage").then((m) => ({ default: m.SettingsRiskRulesPage })))
const SettingsUsersPage = lazy(() => import("@/pages/SettingsUsersPage").then((m) => ({ default: m.SettingsUsersPage })))

const pageFallback = (
  <div className="flex min-h-svh items-center justify-center text-sm text-muted-foreground">
    Loading…
  </div>
)

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
      retry: false,
    },
  },
})

function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <AuthProvider>
          <ActingCompanyProvider>
          <Suspense fallback={pageFallback}>
          <Routes>
            <Route path="/login" element={<LoginPage />} />

            <Route element={<ProtectedRoute />}>
              {/* Core Case Management */}
              <Route path="/" element={<CaseQueuePage />} />
              <Route path="/cases" element={<CaseQueuePage />} />
              <Route path="/review-queue" element={<CaseQueuePage />} />
              <Route path="/cases/new" element={<NewCasePage />} />
              <Route path="/cases/bulk" element={<BulkUploadPage />} />
              <Route path="/bulk-uploads" element={<BulkUploadsListPage />} />
              <Route path="/bulk-uploads/:bulkUploadId" element={<BulkUploadDetailPage />} />
              <Route path="/cases/:caseId" element={<CaseDetailPage />} />

              {/* Operational Dashboard & My Cases */}
              <Route path="/dashboard" element={<DashboardPage />} />
              <Route path="/my-cases" element={<MyCasesPage />} />

              {/* Governance & Audit — company reviewers, and platform admins (per company) */}
              <Route element={<RoleRoute minRole="reviewer_l1" platformAdmin="allow" label="Reviewer" />}>
                <Route path="/audit-history" element={<AuditHistoryPage />} />
              </Route>

              {/* Company settings — the company's Reviewer L2s, and platform admins */}
              <Route element={<RoleRoute minRole="reviewer_l2" platformAdmin="allow" label="Reviewer L2" />}>
                <Route path="/settings" element={<Navigate to="/settings/issuer-registry" replace />} />
                <Route path="/settings/issuer-registry" element={<SettingsIssuerRegistryPage />} />
                <Route path="/settings/risk-rules" element={<SettingsRiskRulesPage />} />
              </Route>

              {/* Platform — platform admins only (users, companies, billing, queues) */}
              <Route element={<RoleRoute platformAdmin="only" label="Platform admin" />}>
                <Route path="/settings/users" element={<SettingsUsersPage />} />
                <Route path="/platform" element={<Navigate to="/platform/companies" replace />} />
                <Route path="/platform/companies" element={<PlatformCompaniesPage />} />
                <Route path="/platform/usage" element={<PlatformUsagePage />} />
                <Route path="/platform/queues" element={<PlatformQueuesPage />} />
                <Route path="/platform/rule-templates" element={<PlatformRuleTemplatesPage />} />
              </Route>
            </Route>

            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
          </Suspense>
          </ActingCompanyProvider>
        </AuthProvider>
      </BrowserRouter>
    </QueryClientProvider>
  )
}

export default App
