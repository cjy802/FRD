import { createApp } from 'vue'
import { setTheme } from '@fluentui/web-components'
import { webLightTheme } from '@fluentui/tokens'
import '@fluentui/web-components/button.js'
import App from './App.vue'
import './style.css'

setTheme(webLightTheme)
createApp(App).mount('#app')
