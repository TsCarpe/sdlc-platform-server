# SDLC 平台 K8s 模板（EXEC-PLAN-K8s 阶段 1）——从 hrsystem/k8s.tpl 派生，差异清单：
#   删：NUM/jvm_options/APP_JAR 三个占位符、podAntiAffinity（单副本无意义）、OTel java
#       注入、19642 metrics 端口、hostPath applogs 日志卷 + subPathExpr、SERVER_PORT/OTEL_*/POD_* env
#   改：replicas=1 写死、strategy=Recreate、tcpSocket 探针 → httpGet、preStop → agent pause
#   增：PVC /data 挂载、initContainer 建目录、envFrom ConfigMap/Secret、securityContext 分层
# 渲染：aliprod.groovy（或 runbook §5 的手工 sed 命令），占位符 5 个，sed 用 | 分隔符防镜像 URL 斜杠。
---
apiVersion: v1
kind: Service
metadata:
  name: {{APP_NAME}}
  namespace: {{NAMESPACE}}
  labels:
    app: {{APP_NAME}}
spec:
  type: ClusterIP
  ports:
    - port: {{PORT}}
      targetPort: 8888
      protocol: TCP
      name: http
  selector:
    app: {{APP_NAME}}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{APP_NAME}}
  namespace: {{NAMESPACE}}
  labels:
    app: {{APP_NAME}}
spec:
  # 硬约束勿改：db 层 RLock 进程内全局串行 + AgentLoop 进程内任务执行——双副本即双进程，
  # 任务原子 claim 单通道与「SELECT 守卫→UPDATE」TOCTOU 防护全部失效（EXEC-PLAN-K8s §3.5）
  replicas: 1
  # RWO PVC 单副本下 RollingUpdate 新旧并存挂不上卷；且双进程是红线（同上）。停机窗口为既定代价。
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: {{APP_NAME}}
  template:
    metadata:
      labels:
        app: {{APP_NAME}}
        logType: app    # 公司日志采集标签（容器日志走 stdout；若运维定 hostPath applogs 再补卷）
    spec:
      enableServiceLinks: false
      nodeSelector:
        node.apptype: web
      imagePullSecrets:
        - name: swr-secret
      securityContext:
        fsGroup: 1000   # PVC /data 归 appuser(1000)；runAs* 不放 pod 级——见 init 容器注释
      terminationGracePeriodSeconds: 120
      volumes:
        - name: host-time
          hostPath:
            path: /etc/localtime    # 公司惯例冗余件：TZ env + tzdata 已生效，不以此为依赖
        - name: data
          persistentVolumeClaim:
            claimName: sdlc-platform-data
      initContainers:
        - name: init-data
          image: {{IMAGE_URL}}:{{IMAGE_TAG}}
          imagePullPolicy: IfNotPresent
          # resolve_paths 对绝对路径不建目录（config.py 代码事实）——/data 两根必须启动前就位
          command: ["sh", "-c", "mkdir -p /data/media /data/ws && chown -R 1000:1000 /data"]
          securityContext:
            # 显式 root 且关闭 runAsNonRoot：若把 runAsNonRoot: true 提到 pod 级，会连带作用于
            # init 容器 → CreateContainerConfigError，PVC 永远初始化不了（审查 P0 教训，勿合并到 pod 级）
            runAsUser: 0
            runAsGroup: 0
            runAsNonRoot: false
          volumeMounts:
            - name: data
              mountPath: /data
      containers:
        - name: {{APP_NAME}}
          image: {{IMAGE_URL}}:{{IMAGE_TAG}}
          imagePullPolicy: IfNotPresent
          securityContext:
            runAsNonRoot: true
            runAsUser: 1000
            runAsGroup: 1000
          ports:
            - containerPort: 8888
              name: http
          env:
            - name: TZ
              value: Asia/Shanghai
          envFrom:
            - configMapRef:
                name: sdlc-platform-config
            - secretRef:
                name: sdlc-platform-secret
          volumeMounts:
            - name: host-time
              mountPath: /etc/localtime
              readOnly: true
            - name: data
              mountPath: /data
          readinessProbe:
            httpGet:
              path: /api/health/ready   # 含 MySQL 连通性（DB 不可达摘流不重启）
              port: http
            initialDelaySeconds: 10
            timeoutSeconds: 3
            periodSeconds: 10
            successThreshold: 1
            failureThreshold: 3
          livenessProbe:
            httpGet:
              path: /api/health         # 静态端点：DB 抖动不引发重启风暴（与 readiness 分工）
              port: http
            initialDelaySeconds: 60
            timeoutSeconds: 5
            periodSeconds: 30
            failureThreshold: 3
          startupProbe:
            httpGet:
              path: /api/health
              port: http
            periodSeconds: 5
            timeoutSeconds: 5
            failureThreshold: 24    # ≤120s：首启建库全量迁移余量（常态秒级）
          lifecycle:
            preStop:
              # 停领新任务（幂等失败容忍）；在跑任务由启动期 expire_stale + sweeper 300s 兜底回收
              exec:
                command: ["/bin/sh", "-c", "/opt/app/.venv/bin/python /opt/app/prestop.py || true; sleep 5"]
          resources:
            requests:
              cpu: 250m
              memory: 1Gi
            limits:
              cpu: "2"
              memory: 3Gi
