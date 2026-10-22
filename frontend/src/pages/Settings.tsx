import { KeyRound, ShieldCheck, UserPlus } from "lucide-react"
import { useState, type FormEvent } from "react"
import { PageHeader, Section } from "@/components/common/PageHeader"
import { ErrorState, LoadingBlock, errorMessage } from "@/components/common/states"
import { useToast } from "@/components/common/toast"
import { Alert } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input, Label, Select } from "@/components/ui/input"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { can, useChangeUser, useCreateUser, useSession, useSettings, useUsers } from "@/hooks/queries"
import { dateTime } from "@/lib/format"

function Row({ name, value, note }: { name: string; value: string; note?: string }) {
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-1 border-b py-3 last:border-0">
      <dt className="text-sm font-medium">{name}</dt>
      <dd className="text-right text-sm">
        {value}
        {note && <span className="block text-xs text-muted-foreground">{note}</span>}
      </dd>
    </div>
  )
}

function Accounts() {
  const toast = useToast()
  const users = useUsers(true)
  const create = useCreateUser()
  const change = useChangeUser()
  const [username, setUsername] = useState("")
  const [password, setPassword] = useState("")
  const [role, setRole] = useState("reviewer")

  const submit = (event: FormEvent) => {
    event.preventDefault()
    create.mutate(
      { username, password, role },
      {
        onSuccess: () => {
          toast.success(`Created ${username.trim().toLowerCase()}`)
          setUsername("")
          setPassword("")
        },
        onError: (error) => toast.error(errorMessage(error)),
      },
    )
  }

  return (
    <div className="space-y-6">
      {users.isPending && <LoadingBlock rows={3} label="Loading accounts" />}
      {users.isError && <ErrorState error={users.error} onRetry={() => users.refetch()} />}

      {users.data && (
        <Table>
          <TableHeader>
            <tr>
              <TableHead>User</TableHead>
              <TableHead>Role</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Password changed</TableHead>
              <TableHead>Actions</TableHead>
            </tr>
          </TableHeader>
          <TableBody>
            {users.data.map((user) => (
              <TableRow key={user.username}>
                <TableCell className="font-medium">{user.username}</TableCell>
                <TableCell>{user.role === "admin" ? "Administrator" : "Auditor / reviewer"}</TableCell>
                <TableCell>{user.active ? <Badge tone="ok">Active</Badge> : <Badge>Inactive</Badge>}{user.locked_until ? <Badge tone="warn" className="ml-1">Locked</Badge> : null}</TableCell>
                <TableCell className="text-xs">{dateTime(user.password_changed_at)}</TableCell>
                <TableCell>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={change.isPending}
                    onClick={() =>
                      change.mutate({ username: user.username, active: !user.active }, { onSuccess: () => toast.success(`${user.username} ${user.active ? "deactivated" : "activated"}; their sessions ended.`), onError: (error) => toast.error(errorMessage(error)) })
                    }
                  >
                    {user.active ? "Deactivate" : "Activate"}
                  </Button>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <UserPlus className="size-4 text-muted-foreground" aria-hidden="true" /> Create an account
          </CardTitle>
          <CardDescription>Passwords are stored only as salted hashes and have at least 12 characters.</CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={submit} className="grid gap-3 sm:grid-cols-4 sm:items-end">
            <div className="space-y-1.5">
              <Label htmlFor="new-username">User name</Label>
              <Input id="new-username" value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="off" />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="new-password">Password</Label>
              <Input id="new-password" type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="new-password" />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="new-role">Role</Label>
              <Select id="new-role" value={role} onChange={(event) => setRole(event.target.value)}>
                <option value="reviewer">Auditor / reviewer</option>
                <option value="admin">Administrator</option>
              </Select>
            </div>
            <Button type="submit" disabled={create.isPending || !username.trim() || password.length < 12}>
              <KeyRound /> Create
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  )
}

export function SettingsPage() {
  const settings = useSettings()
  const session = useSession()
  const isAdmin = can(session.data, "admin.users")

  return (
    <>
      <PageHeader title="Settings" description="How this installation is configured. These options are set on the server and cannot be changed from the browser." />

      {settings.isPending && <LoadingBlock rows={5} label="Loading settings" />}
      {settings.isError && <ErrorState error={settings.error} onRetry={() => settings.refetch()} />}

      {settings.data && (
        <Section title="Configuration">
          <Card>
            <CardContent className="pt-3">
              <dl>
                <Row name="Sign-in" value={settings.data.auth_mode === "required" ? "Required" : "Local development mode"} note={settings.data.auth_mode === "required" ? "Every use needs a signed-in account." : "Nobody signs in. Never use this mode on a shared server."} />
                <Row name="Resolutions need approval" value={settings.data.resolution_needs_approval ? "Yes" : "No"} note="When on, an administrator other than the resolver approves a resolution or dismissal." />
                <Row name="Resolutions need evidence" value={settings.data.resolution_needs_evidence ? "Yes" : "No"} />
                <Row name="Shortest resolution or dismissal note" value={`${settings.data.minimum_note_length} characters`} />
                <Row name="Upload limit" value={`${settings.data.upload_limit_mb} MB per file`} note={`Up to ${settings.data.max_files_per_upload} files at a time.`} />
                <Row name="Language model" value={settings.data.language_model} note="Used for questions and optional commentary. It never changes an audit result." />
                <Row name="Embedding model" value={settings.data.embedding_model} />
                <Row name="OCR" value="Not available" note={settings.data.ocr} />
                <Row name="Currencies" value="Never converted or mixed" note={settings.data.currency_handling} />
              </dl>
            </CardContent>
          </Card>
          <p className="mt-3 flex items-center gap-2 text-xs text-muted-foreground">
            <ShieldCheck className="size-4" aria-hidden="true" /> Paths, databases and secrets are never shown here.
          </p>
        </Section>
      )}

      <Section title="Accounts" description="Administrators manage accounts. Deactivating an account ends its sessions at once.">
        {session.data?.mode !== "required" ? (
          <Alert tone="info"><p>Local development mode has no user accounts. Set LOGIDOC_AUTH_MODE=required on the server and create accounts with scripts/manage_users.py.</p></Alert>
        ) : isAdmin ? (
          <Accounts />
        ) : (
          <Alert tone="info"><p>Account management is for administrators.</p></Alert>
        )}
      </Section>
    </>
  )
}
