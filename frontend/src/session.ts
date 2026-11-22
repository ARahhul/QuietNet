/**
 * Who is using this browser: a Firebase sign-in (one QuietNet account per home), or the server's access token.
 * Components read it with useSession(); the dashboard shows only when someone is signed in.
 */
import { useSyncExternalStore } from 'react'
import { api, ApiError, AUTH_EVENT, forgetToken, getToken, hasSavedToken, REJECTED_EVENT } from './api'
import { firebaseEnabled, firebaseState, onFirebaseChange, signOutOfFirebase } from './lib/firebase'

export type Session =
  | { status: 'loading' }
  | { status: 'signed-out'; notice?: string }
  | { status: 'signed-in'; via: 'firebase'; uid: string; email: string | null; name: string | null; photo: string | null }
  | { status: 'signed-in'; via: 'token' }

export { firebaseEnabled }

let notice: string | undefined
let signedOut = false // ignore the dev default token after "Sign out", until the next sign-in
let claiming = false
let movedHome = false
let snapshot: Session = compute()
const subscribers = new Set<() => void>()
const accountChange = new Set<() => void>()

function compute(): Session {
  const fb = firebaseState()
  if (!fb.ready || claiming) return { status: 'loading' }
  if (fb.user) {
    const u = fb.user
    return { status: 'signed-in', via: 'firebase', uid: u.uid, email: u.email, name: u.displayName, photo: u.photoURL }
  }
  if (getToken() && (hasSavedToken() || !signedOut)) return { status: 'signed-in', via: 'token' }
  return { status: 'signed-out', notice }
}

const identity = (s: Session) => (s.status === 'signed-in' ? (s.via === 'firebase' ? s.uid : 'token') : s.status)

function update() {
  const next = compute()
  if (JSON.stringify(next) === JSON.stringify(snapshot)) return
  const switched = identity(next) !== identity(snapshot) && next.status !== 'loading'
  snapshot = next
  if (switched) accountChange.forEach((f) => f())
  subscribers.forEach((f) => f())
}

/**
 * The browser that used the server's access token before sign-in existed: on its first sign-in, the home set up
 * with the token moves into the signed-in account (once), and the token is dropped from this browser.
 */
async function claimSavedToken() {
  if (!firebaseState().user || !hasSavedToken() || claiming) return
  claiming = true
  update()
  try {
    const account = await api.account()
    if (account.can_claim) {
      await api.claim(getToken())
      movedHome = true
    }
    localStorage.removeItem('quietnet.token')
  } catch (e) {
    // Keep the token if the server couldn't be asked; a refused claim (wrong token, already moved) drops it.
    if (e instanceof ApiError && e.status >= 400 && e.status < 500 && e.status !== 401) localStorage.removeItem('quietnet.token')
  } finally {
    claiming = false
    update()
  }
}

onFirebaseChange(() => {
  if (firebaseState().user) {
    notice = undefined
    signedOut = false
  }
  update()
  void claimSavedToken()
})
if (firebaseState().ready) void claimSavedToken()

window.addEventListener(AUTH_EVENT, () => {
  if (hasSavedToken()) {
    notice = undefined
    signedOut = false
  }
  update()
})
window.addEventListener('storage', (e) => {
  if (e.key === null || e.key === 'quietnet.token') update()
})
window.addEventListener(REJECTED_EVENT, () => {
  if (firebaseState().user) {
    notice = 'QuietNet’s server didn’t accept your sign-in. Try again in a minute.'
    void signOutOfFirebase()
  } else {
    notice = hasSavedToken() ? 'That access token no longer works. Sign in again.' : undefined
    signedOut = true
    forgetToken()
  }
  update()
})

export function useSession(): Session {
  return useSyncExternalStore(
    (f) => {
      subscribers.add(f)
      return () => subscribers.delete(f)
    },
    () => snapshot,
  )
}

/** Runs before the app re-renders for a different person (so nothing cached from the last one shows). */
export function onAccountChange(f: () => void) {
  accountChange.add(f)
  return () => accountChange.delete(f)
}

/** True once, right after the token's home moved into this sign-in. */
export function takeMovedHome(): boolean {
  const m = movedHome
  movedHome = false
  return m
}

export async function signOut() {
  notice = undefined
  signedOut = true
  if (hasSavedToken()) forgetToken()
  await signOutOfFirebase()
  update()
}
