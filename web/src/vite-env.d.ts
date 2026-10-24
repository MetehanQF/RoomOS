/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_ROOMOS_RELEASE?: string
  readonly VITE_ROOMOS_REVISION?: string
  readonly VITE_ROOMOS_COMPONENT?: 'web'
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
