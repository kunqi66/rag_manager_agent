import { createApp } from 'vue'
import { createPinia } from 'pinia'

import App from './App.vue'
import router from './router'
import { setupGuards } from './router/guards'
import { permission } from './directives/permission'

import './styles/tokens.css'
import './styles/index.css'

const app = createApp(App)

// pinia 必须先于 router 安装：路由守卫里要 useAuthStore()
app.use(createPinia())
app.use(router)

// v-permission 只控制显隐，不是安全边界（CLAUDE.md 安全红线 7）
app.directive('permission', permission)

setupGuards(router)

app.mount('#app')
