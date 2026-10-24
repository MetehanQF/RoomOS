export type PwaUpdateState = 'current' | 'available'

export function isStandalone(matchMedia = window.matchMedia.bind(window), navigatorLike = navigator) {
  return matchMedia('(display-mode: standalone)').matches || ('standalone' in navigatorLike && Boolean((navigatorLike as Navigator & { standalone?: boolean }).standalone))
}

type ServiceWorkerContainerLike = Pick<ServiceWorkerContainer, 'register' | 'getRegistration' | 'controller' | 'addEventListener' | 'removeEventListener'>

export function installPwaLifecycle(
  onUpdate: (state: PwaUpdateState) => void,
  container: ServiceWorkerContainerLike | null = 'serviceWorker' in navigator ? navigator.serviceWorker : null,
  reload = () => window.location.reload(),
) {
  if (!container) return () => undefined
  let refreshing = false
  let controlled = Boolean(container.controller)
  const controllerChanged = () => {
    // First installation only claims this already-current page. Reloading here duplicates
    // auth/bootstrap requests. A later replacement still follows the explicit update flow.
    if (!controlled) { controlled = true; return }
    if (refreshing) return
    refreshing = true
    reload()
  }
  container.addEventListener('controllerchange', controllerChanged)
  void container.register('/sw.js').then((registration) => {
    const inspect = () => {
      if (shouldOfferUpdate(registration.waiting, container.controller)) onUpdate('available')
    }
    inspect()
    registration.addEventListener('updatefound', () => {
      const worker = registration.installing
      worker?.addEventListener('statechange', () => {
        if (worker.state === 'installed') inspect()
      })
    })
  }).catch(() => undefined)
  return () => container.removeEventListener('controllerchange', controllerChanged)
}

export function shouldOfferUpdate(waiting: ServiceWorker | null, controller: ServiceWorker | null) {
  return Boolean(waiting && controller)
}

export async function activateWaitingWorker(container: Pick<ServiceWorkerContainer, 'getRegistration'> = navigator.serviceWorker) {
  const registration = await container.getRegistration()
  registration?.waiting?.postMessage({ type: 'SKIP_WAITING' })
}
