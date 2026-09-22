<script setup>
import { EditPen, Monitor, Notebook, School, Trophy } from '@element-plus/icons-vue'
import { computed } from 'vue'
import { useRoute } from 'vue-router'

const route = useRoute()

const menus = [
  { index: '/', label: '概览', icon: Monitor },
  { index: '/classes', label: '班级与学生', icon: School },
  { index: '/rubrics', label: '评分细则', icon: Notebook },
  { index: '/grading', label: '评审与成绩', icon: EditPen },
]

const activeMenu = computed(() => {
  if (route.path.startsWith('/rubrics')) return '/rubrics'
  if (route.path.startsWith('/grading')) return '/grading'
  if (route.path.startsWith('/classes')) return '/classes'
  return '/'
})
</script>

<template>
  <el-container style="height: 100%">
    <el-aside width="216px" style="background: #1f2d3d">
      <div
        style="
          color: #fff;
          font-size: 16px;
          font-weight: 600;
          padding: 20px 18px;
          display: flex;
          align-items: center;
          gap: 8px;
        "
      >
        <el-icon><Trophy /></el-icon>
        <span>作业自动评审</span>
      </div>
      <el-menu :default-active="activeMenu" router background-color="#1f2d3d" text-color="#c0c4cc" active-text-color="#ffd04b">
        <el-menu-item v-for="item in menus" :key="item.index" :index="item.index">
          <el-icon><component :is="item.icon" /></el-icon>
          <span>{{ item.label }}</span>
        </el-menu-item>
      </el-menu>
    </el-aside>
    <el-main style="padding: 0; background: #f5f7fa">
      <router-view />
    </el-main>
  </el-container>
</template>
