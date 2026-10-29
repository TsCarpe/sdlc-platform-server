// SDLC 平台部署（阿里生产 ACK）——结构对齐 hrsystem/aliprod.groovy。
// 与 hrsystem 的差异：① 不调 common-shared-library 的 deploy_aliack_cluster_jdk21（JDK 专用且
// 无 PVC/ConfigMap 渲染能力；待平台侧补 Python/带卷变体后可切换 @Library + 变体调用，本文件
// 届时退化为一次函数调用）；② 镜像由本地 deploy/build.sh 构建推送 SWR（CI 暂跳过，EXEC-PLAN-K8s
// §8-6 拍板），本流水线只做渲染 + apply；③ 前置一次性件（namespace/PVC/ConfigMap/Secret）按
// runbook §3 手工准备，不在流水线内。
pipeline {
    agent any
    parameters {
        string(name: 'NAMESPACE', defaultValue: 'sdlc', description: '目标 namespace（归属待运维确认）')
        string(name: 'IMAGE_TAG', defaultValue: '', description: '镜像 tag——取 deploy/build.sh 输出的 IMAGE_TAG')
        string(name: 'APP_NAME', defaultValue: 'sdlc-platform', description: '应用名（Service/Deployment 共用）')
        string(name: 'PORT', defaultValue: '8888', description: 'Service 端口')
        string(name: 'IMAGE_URL', defaultValue: 'swr.cn-north-4.myhuaweicloud.com/hlyz/sdlc-platform-server', description: '镜像地址（region 终值待运维确认）')
        string(name: 'KUBECONFIG_ID', defaultValue: 'sdlc-kubeconfig', description: 'kubeconfig 文件凭据 id')
    }
    stages {
        stage('Render') {
            steps {
                sh '''
                    sed -e "s|{{APP_NAME}}|${APP_NAME}|g" \
                        -e "s|{{NAMESPACE}}|${NAMESPACE}|g" \
                        -e "s|{{PORT}}|${PORT}|g" \
                        -e "s|{{IMAGE_URL}}|${IMAGE_URL}|g" \
                        -e "s|{{IMAGE_TAG}}|${IMAGE_TAG}|g" \
                        deploy/k8s.tpl > deploy/k8s.rendered.yaml
                    echo "== 渲染产物 ==" && cat deploy/k8s.rendered.yaml
                '''
            }
        }
        stage('Apply') {
            steps {
                withCredentials([file(credentialsId: "${KUBECONFIG_ID}", variable: 'KUBECONFIG')]) {
                    sh 'kubectl apply -f deploy/k8s.rendered.yaml -n ${NAMESPACE}'
                }
            }
        }
    }
    post {
        success {
            withCredentials([file(credentialsId: "${KUBECONFIG_ID}", variable: 'KUBECONFIG')]) {
                // 300s > 最坏组合「旧 pod 拖满 120s grace + 新 pod startup 预算 120s」——正常滚动不被误标失败
                sh 'kubectl rollout status deployment/${APP_NAME} -n ${NAMESPACE} --timeout=300s'
            }
        }
    }
}
