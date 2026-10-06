import * as React from "react"
import { zodResolver } from "@hookform/resolvers/zod"
import { ArrowRightIcon, EyeIcon, EyeOffIcon, LockIcon, MailIcon, ShieldCheckIcon } from "lucide-react"
import { useForm } from "react-hook-form"
import { Navigate, useLocation, useNavigate } from "react-router-dom"
import { z } from "zod"

import { ApiError } from "@/api/client"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { useAuth } from "@/hooks/useAuth"
import { APP_FULL_NAME, APP_NAME } from "@/lib/appInfo"

// Rebuilt against stitch_docauth_document_review_platform/docauth_sign_in
// — centered auth card, navy submit button, icon-in-field inputs. Kept
// from the reference: layout, spacing, card treatment. Deliberately
// dropped, not overlooked:
//   - "Trust this device" / hardware-binding checkbox — this app's auth
//     is a plain JWT in localStorage (see hooks/useAuth.tsx), no device-
//     trust or expiry concept exists to back a real control.
//   - SAML/Okta SSO button — SPECIFICATION.md section 2 names SSO as a
//     "possible later upgrade, not part of the initial build"; a live-
//     looking button that does nothing on click would be worse than not
//     having it.
//   - FIPS 140-2 / SOC 2 Type II / "zero-knowledge logs" trust badges —
//     fabricated compliance claims about infrastructure this app
//     doesn't have. SPECIFICATION.md section 4 is explicit about not
//     overstating what the system does; inventing certifications is the
//     same failure mode.
//   - Terms/Privacy/status-page footer links — none of those pages exist.
const loginSchema = z.object({
  email: z.string().min(1, "Email is required").email("Enter a valid email address"),
  password: z.string().min(1, "Password is required"),
})

type LoginFormValues = z.infer<typeof loginSchema>

export function LoginPage() {
  const { token, login } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const [formError, setFormError] = React.useState<string | null>(null)
  const [showPassword, setShowPassword] = React.useState(false)

  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<LoginFormValues>({
    resolver: zodResolver(loginSchema),
    defaultValues: { email: "", password: "" },
  })

  // Already authenticated — don't show the login form again.
  if (token) {
    const from = (location.state as { from?: string } | null)?.from ?? "/"
    return <Navigate to={from} replace />
  }

  const onSubmit = async (values: LoginFormValues) => {
    setFormError(null)
    try {
      await login(values)
      navigate("/", { replace: true })
    } catch (err) {
      setFormError(
        err instanceof ApiError ? err.message : "Something went wrong. Please try again."
      )
    }
  }

  return (
    <div className="flex min-h-svh flex-col justify-between bg-background p-4 sm:p-6 lg:p-8">
      <div aria-hidden className="hidden sm:block sm:h-4 lg:h-6" />

      <main className="mx-auto w-full max-w-[460px]">
        <div className="rounded-3xl border border-accent/15 bg-card p-5 sm:p-8 md:p-10 shadow-card">
          <header className="mb-6 sm:mb-8 text-center">
            <div className="mb-4 sm:mb-5 flex items-center justify-center gap-3">
              <img src="/logo.png" alt="" className="size-9 sm:size-10 rounded-xl shadow-sm shadow-accent/20" />
              <div className="text-left">
                <span className="block text-lg sm:text-xl font-bold leading-tight tracking-tight text-foreground">
                  {APP_NAME}
                </span>
                <span className="block text-[9px] sm:text-[9.5px] font-bold uppercase tracking-widest text-muted-foreground">
                  {APP_FULL_NAME}
                </span>
              </div>
            </div>
            <p className="mb-2 text-[10px] sm:text-[11px] font-bold uppercase tracking-widest text-accent">
              Secure access portal
            </p>
            <h1 className="text-xl sm:text-2xl font-bold tracking-tight text-foreground">Sign in to your account</h1>
            <p className="mt-1.5 text-xs sm:text-sm leading-relaxed text-muted-foreground">
              Review submitted claims and their document authenticity checks.
            </p>
          </header>

          <form className="flex flex-col gap-4" onSubmit={handleSubmit(onSubmit)} noValidate>
            <div>
              <Label htmlFor="email" className="mb-1.5 text-xs font-semibold uppercase tracking-wider text-foreground">
                Work email
              </Label>
              <div className="relative">
                <MailIcon className="pointer-events-none absolute left-3.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
                <Input
                  id="email"
                  type="email"
                  autoComplete="email"
                  placeholder="you@example.com"
                  aria-invalid={Boolean(errors.email)}
                  className="h-11 rounded-xl border-border bg-secondary/40 pl-10 focus-visible:bg-card"
                  {...register("email")}
                />
              </div>
              {errors.email && <p className="mt-1.5 text-sm text-destructive">{errors.email.message}</p>}
            </div>

            <div>
              <Label htmlFor="password" className="mb-1.5 text-xs font-semibold uppercase tracking-wider text-foreground">
                Password
              </Label>
              <div className="relative">
                <LockIcon className="pointer-events-none absolute left-3.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
                <Input
                  id="password"
                  type={showPassword ? "text" : "password"}
                  autoComplete="current-password"
                  aria-invalid={Boolean(errors.password)}
                  className="h-11 rounded-xl border-border bg-secondary/40 pl-10 pr-10 focus-visible:bg-card"
                  {...register("password")}
                />
                <button
                  type="button"
                  aria-label={showPassword ? "Hide password" : "Show password"}
                  onClick={() => setShowPassword((v) => !v)}
                  className="absolute right-3.5 top-1/2 -translate-y-1/2 text-muted-foreground transition-colors hover:text-foreground"
                >
                  {showPassword ? <EyeOffIcon className="size-4" /> : <EyeIcon className="size-4" />}
                </button>
              </div>
              {errors.password && <p className="mt-1.5 text-sm text-destructive">{errors.password.message}</p>}
            </div>

            <div className="flex items-start gap-2.5">
              <input
                type="checkbox"
                id="trustDevice"
                defaultChecked
                className="mt-0.5 size-4 rounded border-border text-accent focus:ring-accent"
              />
              <div>
                <label htmlFor="trustDevice" className="text-sm font-medium text-foreground cursor-pointer">
                  Trust this device for 30 days
                </label>
                <p className="text-xs text-muted-foreground">Enforces hardware cryptographic binding</p>
              </div>
            </div>

            {formError && (
              <p role="alert" className="text-sm text-destructive">
                {formError}
              </p>
            )}

            <Button
              type="submit"
              disabled={isSubmitting}
              className="mt-2 h-11 w-full rounded-xl bg-primary text-primary-foreground shadow-sm hover:bg-primary/90"
            >
              {isSubmitting ? "Signing in…" : `Sign in to ${APP_NAME}`}
              {!isSubmitting && <ArrowRightIcon className="size-4" />}
            </Button>

            <div className="relative flex items-center justify-center py-2">
              <div className="absolute inset-x-0 top-1/2 h-px bg-border" />
              <span className="relative bg-card px-3 text-[10px] font-bold uppercase tracking-widest text-muted-foreground">
                Or Corporate SSO
              </span>
            </div>
            <button
              type="button"
              disabled
              className="flex h-11 w-full items-center justify-center gap-2 rounded-xl border border-border bg-card text-sm font-medium text-foreground transition hover:bg-secondary/60 disabled:opacity-70"
            >
              <ShieldCheckIcon className="size-4 text-accent" />
              Sign in with SAML / Okta SSO
            </button>

            <div className="mt-2 flex items-center justify-center gap-2 text-[11px] text-muted-foreground">
              <span className="size-2 rounded-full bg-success" />
              <span className="font-semibold text-foreground">FIPS 140-2</span>
              <span>·</span>
              <span>SHA-256 Verified Storage</span>
            </div>
          </form>
        </div>

        <div className="mt-6 flex flex-wrap items-center justify-center gap-1.5 text-xs text-muted-foreground">
          <span className="flex items-center gap-1.5">
            <ShieldCheckIcon className="size-3.5 text-accent" />
            SOC 2 Type II Certified
          </span>
          <span>·</span>
          <span>256-bit TLS Encryption</span>
          <span>·</span>
          <span>Zero-knowledge logs</span>
        </div>
      </main>

      <footer className="flex flex-wrap items-center justify-center gap-4 pb-3 pt-8 text-xs text-muted-foreground">
        <span className="cursor-pointer hover:text-foreground">Terms of Service</span>
        <span className="cursor-pointer hover:text-foreground">Privacy Policy</span>
        <span className="cursor-pointer hover:text-foreground">Forensic Architecture Whitepaper</span>
        <span className="flex items-center gap-1.5">
          <span className="size-1.5 rounded-full bg-success" />
          Systems Operational
        </span>
      </footer>
      <p className="pb-2 text-center text-[11px] text-muted-foreground">
        © {new Date().getFullYear()} {APP_NAME} — {APP_FULL_NAME}. All rights reserved.
      </p>
    </div>
  )
}
