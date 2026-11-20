FROM eclipse-temurin:21.0.11_10-jre-jammy@sha256:9af01c30e85b5a9b15fe249a84fb6dcb19e06ee84fbf23ea2d116b0e195c2ce9
WORKDIR /opt/supportflow
COPY target/spring-sandbox.jar /opt/supportflow/sandbox.jar
USER 10001:10001
EXPOSE 8001
ENTRYPOINT ["java", "-jar", "/opt/supportflow/sandbox.jar"]
