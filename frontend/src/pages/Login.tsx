import { LogIn } from "lucide-react"
import { useState, type FormEvent } from "react"
import { Alert } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Input, Label } from "@/components/ui/input"
import { useLogin } from "@/hooks/queries"
import { errorMessage } from "@/components/common/states"

export function Login() {
  const login = useLogin()
  const [username, setUsername] = useState("")
  const [password, setPassword] = useState("")

  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (!username.trim() || !password) return
    login.mutate({ username: username.trim(), password })
  }

  return (
    <div className="grid min-h-screen place-items-center bg-navy p-4">
      <Card className="w-full max-w-sm p-7">
        <div className="mb-6">
          <h1 className="text-xl font-semibold">Sign in to LogiDoc</h1>
          <p className="mt-1 text-sm text-muted-foreground">Freight audit intelligence for your shipment documents.</p>
        </div>

        <form onSubmit={submit} className="space-y-4" noValidate>
          <div className="space-y-1.5">
            <Label htmlFor="username">User name</Label>
            <Input id="username" name="username" autoComplete="username" autoFocus value={username} onChange={(event) => setUsername(event.target.value)} aria-required="true" />
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="password">Password</Label>
            <Input id="password" name="password" type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} aria-required="true" />
          </div>

          {login.isError && <Alert tone="bad">{errorMessage(login.error)}</Alert>}

          <Button type="submit" className="w-full" disabled={login.isPending || !username.trim() || !password}>
            <LogIn /> {login.isPending ? "Signing in…" : "Sign in"}
          </Button>
        </form>

        <p className="mt-5 text-xs text-muted-foreground">Accounts are created by the administrator of this installation. Use a secure (HTTPS) connection.</p>
      </Card>
    </div>
  )
}
