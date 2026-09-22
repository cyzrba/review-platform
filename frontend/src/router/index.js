import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  { path: '/', name: 'dashboard', component: () => import('@/views/Dashboard.vue') },
  { path: '/classes', name: 'classes', component: () => import('@/views/Classes.vue') },
  { path: '/rubrics', name: 'rubrics', component: () => import('@/views/Rubrics.vue') },
  { path: '/grading', name: 'grading', component: () => import('@/views/Grading.vue') },
  { path: '/grading/:rubricId', name: 'grading-detail', component: () => import('@/views/Grading.vue') },
]

export default createRouter({
  history: createWebHistory(),
  routes,
})
