<script setup lang="ts">
defineProps<{status?:string}>()
const labels:Record<string,string>={ok:'正常',partial:'部分异常',stale:'旧缓存',error:'失败',empty:'未更新'}
</script>
<template><span class="status-tag" :class="status||'empty'"><i></i>{{ labels[status||'empty']||'未更新' }}</span></template>
