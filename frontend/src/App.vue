<script setup>
import {
  ArrowRight,
  Download,
  EditPen,
  Menu,
  Monitor,
  Notebook,
  School,
} from '@element-plus/icons-vue'
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

const route = useRoute()
const menuOpen = ref(false)

const menus = [
  { index: '/', label: '概览', short: '工作台', icon: Monitor },
  { index: '/classes', label: '班级与学生', short: '班级名单', icon: School },
  { index: '/rubrics', label: '评分细则', short: '评分细则', icon: Notebook },
  { index: '/data-sync', label: '数据同步', short: '数据同步', icon: Download },
  { index: '/grading', label: '评审与成绩', short: '评审成绩', icon: EditPen },
]

const activeMenu = computed(() => {
  if (route.path.startsWith('/rubrics')) return '/rubrics'
  if (route.path.startsWith('/data-sync')) return '/data-sync'
  if (route.path.startsWith('/grading')) return '/grading'
  if (route.path.startsWith('/classes')) return '/classes'
  return '/'
})

const activePage = computed(
  () => menus.find((item) => item.index === activeMenu.value)?.short || '工作台',
)

watch(
  () => route.fullPath,
  () => {
    menuOpen.value = false
    window.scrollTo(0, 0)
  },
)
</script>

<template>
  <div class="app-shell">
    <button
      v-if="menuOpen"
      class="mobile-scrim"
      aria-label="关闭导航"
      @click="menuOpen = false"
    />

    <aside class="sidebar" :class="{ 'is-open': menuOpen }">
      <router-link class="brand" to="/" aria-label="返回概览">
        <span class="brand-mark" aria-hidden="true">
          <span></span>
          <span></span>
        </span>
        <span class="brand-copy">
          <strong>Review</strong>
          <small>作业评审平台</small>
        </span>
      </router-link>

      <div class="sidebar-label">工作区</div>
      <nav class="sidebar-nav" aria-label="主导航">
        <router-link
          v-for="item in menus"
          :key="item.index"
          :to="item.index"
          class="nav-item"
          :class="{ 'is-active': activeMenu === item.index }"
        >
          <el-icon><component :is="item.icon" /></el-icon>
          <span>{{ item.label }}</span>
          <i aria-hidden="true"></i>
        </router-link>
      </nav>

      <div class="sidebar-spacer"></div>
      <div class="sidebar-note">
        <span class="note-index">01</span>
        <strong>教师工作台</strong>
        <span>资料保存在本机</span>
      </div>
      <div class="sidebar-user">
        <span class="avatar">教</span>
        <span>
          <strong>教师账号</strong>
          <small>平台管理员</small>
        </span>
      </div>
    </aside>

    <div class="app-main">
      <header class="topbar">
        <div class="topbar-left">
          <button
            class="menu-button"
            :aria-expanded="menuOpen"
            aria-label="打开导航"
            @click="menuOpen = true"
          >
            <el-icon><Menu /></el-icon>
          </button>
          <div class="breadcrumb" aria-label="当前位置">
            <span>Review workspace</span>
            <el-icon><ArrowRight /></el-icon>
            <strong>{{ activePage }}</strong>
          </div>
        </div>
        <div class="topbar-meta">
          <span class="topbar-status"><i></i> AI 辅助评阅</span>
          <span class="topbar-separator"></span>
          <span class="topbar-role">教师端</span>
        </div>
      </header>

      <main class="page-stage">
        <router-view v-slot="{ Component }">
          <transition name="page-fade" mode="out-in">
            <component :is="Component" />
          </transition>
        </router-view>
      </main>
    </div>
  </div>
</template>
