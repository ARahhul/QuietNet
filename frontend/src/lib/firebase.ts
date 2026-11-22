/**
 * Firebase Authentication: people sign in with Google, or with a link sent to their email. QuietNet's server checks
 * the Firebase ID token on every request and shows each sign-in only its own home (backend: pahra/auth.py).
 *
 * Configured at build time (VITE_FIREBASE_*). Without it the dashboard falls back to the server's access token.
 * The web API key is not a secret: it only names the Firebase project, and sign-ins are limited to its authorized domains.
 */
import { initializeApp } from '@firebase/app'
import {
  browserLocalPersistence,
  browserPopupRedirectResolver,
  connectAuthEmulator,
  GoogleAuthProvider,
  indexedDBLocalPersistence,
  initializeAuth,
  isSignInWithEmailLink,
  onIdTokenChanged,
  sendSignInLinkToEmail,
  signInWithEmailLink,
  signInWithPopup,
  signOut as firebaseSignOut,
  type Auth,
  type User,
} from '@firebase/auth'

const env = import.meta.env
const projectId: string = env.VITE_FIREBASE_PROJECT_ID ?? ''
export const firebaseEnabled = !!(env.VITE_FIREBASE_API_KEY && projectId)

let auth: Auth | null = null
if (firebaseEnabled) {
  const app = initializeApp({
    apiKey: env.VITE_FIREBASE_API_KEY,
    authDomain: env.VITE_FIREBASE_AUTH_DOMAIN || `${projectId}.firebaseapp.com`,
    projectId,
    appId: env.VITE_FIREBASE_APP_ID || undefined,
  })
  auth = initializeAuth(app, {
    persistence: [indexedDBLocalPersistence, browserLocalPersistence],
    popupRedirectResolver: browserPopupRedirectResolver,
  })
  // Local development only: the Firebase Auth emulator (firebase emulators:start --only auth)
  if (env.VITE_FIREBASE_AUTH_EMULATOR) connectAuthEmulator(auth, env.VITE_FIREBASE_AUTH_EMULATOR, { disableWarnings: true })
}

export interface FirebaseState {
  ready: boolean
  user: User | null
}
let state: FirebaseState = { ready: !auth, user: null }
let idToken = ''
const listeners = new Set<() => void>()

if (auth) {
  const a = auth
  void a.authStateReady().then(() => {
    state = { ready: true, user: a.currentUser }
    listeners.forEach((l) => l())
  })
  onIdTokenChanged(a, async (user) => {
    idToken = user ? await user.getIdToken() : ''
    if (state.ready && state.user?.uid !== user?.uid) {
      state = { ready: true, user }
      listeners.forEach((l) => l())
    }
  })
}

export const firebaseState = () => state
export function onFirebaseChange(l: () => void) {
  listeners.add(l)
  return () => listeners.delete(l)
}

/** The signed-in person's ID token (refreshed by Firebase before it expires), or '' when nobody is signed in. */
export async function firebaseToken(forceRefresh = false): Promise<string> {
  const user = auth?.currentUser
  if (!user) return ''
  idToken = await user.getIdToken(forceRefresh)
  return idToken
}
/** The latest ID token without waiting, for links and EventSource, which can't send headers. */
export const firebaseTokenNow = () => (auth?.currentUser ? idToken : '')

function need(): Auth {
  if (!auth) throw new Error('Sign-in isn’t set up for this dashboard.')
  return auth
}

export async function signInWithGoogle(): Promise<void> {
  const provider = new GoogleAuthProvider()
  provider.setCustomParameters({ prompt: 'select_account' })
  await signInWithPopup(need(), provider)
}

const EMAIL_KEY = 'quietnet.signInEmail'

/** Email the person a one-time sign-in link back to this page. */
export async function sendEmailLink(email: string): Promise<void> {
  await sendSignInLinkToEmail(need(), email, { url: `${location.origin}/login`, handleCodeInApp: true })
  localStorage.setItem(EMAIL_KEY, email)
}

/** This page was opened from a sign-in link. */
export const isEmailLink = () => !!auth && isSignInWithEmailLink(auth, location.href)
/** The email the link was sent to, when it was sent from this browser. */
export const emailLinkAddress = () => localStorage.getItem(EMAIL_KEY) ?? ''

export async function finishEmailLink(email: string): Promise<void> {
  await signInWithEmailLink(need(), email, location.href)
  localStorage.removeItem(EMAIL_KEY)
}

export async function signOutOfFirebase(): Promise<void> {
  if (auth) await firebaseSignOut(auth)
}

/** Firebase's error codes, said plainly. */
export function signInProblem(e: unknown): string | null {
  const code = (e as { code?: string })?.code ?? ''
  switch (code) {
    case 'auth/popup-closed-by-user':
    case 'auth/cancelled-popup-request':
    case 'auth/user-cancelled':
      return null // they closed it on purpose
    case 'auth/popup-blocked':
      return 'Your browser blocked the Google window. Allow pop-ups for this site, or use the email link instead.'
    case 'auth/invalid-email':
      return 'That email address doesn’t look right.'
    case 'auth/invalid-action-code':
    case 'auth/expired-action-code':
      return 'That sign-in link has expired or was already used. Send yourself a new one.'
    case 'auth/unauthorized-domain':
    case 'auth/operation-not-allowed':
      return 'Sign-in isn’t switched on for this website yet. The QuietNet owner needs to enable it in Firebase.'
    case 'auth/network-request-failed':
      return 'Can’t reach Google. Check your connection and try again.'
    case 'auth/too-many-requests':
    case 'auth/quota-exceeded':
      return 'Too many tries. Wait a few minutes and try again.'
    default:
      return (e as Error)?.message || 'Sign-in didn’t work. Try again.'
  }
}
